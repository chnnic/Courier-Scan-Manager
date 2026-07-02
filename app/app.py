import os
import re
import shutil
import sqlite3
import subprocess
import json
import hashlib
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen
from xml.sax.saxutils import escape


APP_NAME = "CourierScanManager"
APP_VERSION = "1.2.11"
DEFAULT_UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/chnnic/Courier-Scan-Manager/main/manifest.json"
APP_SOURCE_DIR = Path(__file__).resolve().parent
DEFAULT_COMPANY_COLOR = "#0B5CAB"
UNRECOGNIZED_COLOR = "#555555"
BLACKLIST_ALERT_COLOR = "#B42318"
LOCKED_ALERT_COLOR = "#C76F00"
CONFIG_DB_NAME = "courier_config.db"
MONTH_DB_PREFIX = "courier_"
MONTH_DB_PATTERN = re.compile(r"^courier_(\d{4})_(\d{2})\.db$")
ALL_MONTHS_VALUE = "__all_months__"


def get_runtime_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return APP_SOURCE_DIR


def get_legacy_user_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base_dir = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base_dir:
            return Path(base_dir) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


APP_DIR = get_runtime_app_dir()
LEGACY_DB_PATH = APP_SOURCE_DIR / "courier.db"
LEGACY_EXPORT_DIR = APP_SOURCE_DIR / "exports"
LEGACY_USER_DATA_DIR = get_legacy_user_data_dir()
LEGACY_USER_DB_PATH = LEGACY_USER_DATA_DIR / "courier.db"
LEGACY_USER_EXPORT_DIR = LEGACY_USER_DATA_DIR / "exports"
CONFIG_DB_PATH = APP_DIR / CONFIG_DB_NAME
EXPORT_DIR = APP_DIR / "exports"
BACKUP_DIR = APP_DIR / "backups"
ARCHIVE_DIR = APP_DIR / "archives"
UPDATE_DIR = APP_DIR / "updates"
AUTO_BACKUP_PREFIX = "auto_backup_"
MANUAL_BACKUP_PREFIX = "manual_backup_"
PRE_RESTORE_BACKUP_PREFIX = "pre_restore_backup_"
PRE_UPDATE_BACKUP_PREFIX = "pre_update_backup_"
MAX_AUTO_BACKUPS = 30


def month_key_from_date(value: str | datetime | None = None) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m")
    if isinstance(value, str) and len(value) >= 7:
        return value[:7]
    return datetime.now().strftime("%Y-%m")


def month_key_to_db_name(month_key: str) -> str:
    return f"{MONTH_DB_PREFIX}{month_key.replace('-', '_')}.db"


def db_path_for_month(month_key: str) -> Path:
    return APP_DIR / month_key_to_db_name(month_key)


def month_key_from_db_path(db_path: Path) -> str | None:
    match = MONTH_DB_PATTERN.match(db_path.name)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def list_month_db_paths() -> list[Path]:
    return sorted(
        [path for path in APP_DIR.glob(f"{MONTH_DB_PREFIX}*.db") if month_key_from_db_path(path)],
        key=lambda path: month_key_from_db_path(path) or "",
        reverse=True,
    )


def migrate_legacy_exports() -> None:
    for legacy_dir in (LEGACY_EXPORT_DIR, LEGACY_USER_EXPORT_DIR):
        if not legacy_dir.exists():
            continue
        for item in legacy_dir.iterdir():
            target = EXPORT_DIR / item.name
            if target.exists() or not item.is_file():
                continue
            shutil.copy2(item, target)


def migrate_legacy_database() -> None:
    if CONFIG_DB_PATH.exists() or list_month_db_paths():
        return

    legacy_candidates: list[Path] = []
    for candidate in (LEGACY_USER_DB_PATH, LEGACY_DB_PATH):
        if not candidate.exists():
            continue
        if candidate.resolve() == CONFIG_DB_PATH.resolve():
            continue
        legacy_candidates.append(candidate)

    if not legacy_candidates:
        return

    source_path = legacy_candidates[0]
    source_conn = sqlite3.connect(source_path)
    source_conn.row_factory = sqlite3.Row

    try:
        config_conn = sqlite3.connect(CONFIG_DB_PATH)
        try:
            config_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    prefix TEXT NOT NULL UNIQUE,
                    color TEXT NOT NULL DEFAULT '#0B5CAB',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            config_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            config_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blocked_tracking_numbers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracking_number TEXT NOT NULL UNIQUE,
                    entry_type TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            for table_name in ("companies", "settings", "blocked_tracking_numbers"):
                table_exists = source_conn.execute(
                    f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'"
                ).fetchone()
                if not table_exists:
                    continue
                rows = source_conn.execute(f"SELECT * FROM {table_name}").fetchall()
                if not rows:
                    continue
                column_names = rows[0].keys()
                placeholders = ", ".join("?" for _ in column_names)
                columns_sql = ", ".join(column_names)
                config_conn.executemany(
                    f"INSERT OR REPLACE INTO {table_name} ({columns_sql}) VALUES ({placeholders})",
                    [tuple(row[column] for column in column_names) for row in rows],
                )
            config_conn.commit()
        finally:
            config_conn.close()

        month_connections: dict[str, sqlite3.Connection] = {}

        def ensure_month_connection(month_key: str) -> sqlite3.Connection:
            if month_key in month_connections:
                return month_connections[month_key]
            month_path = db_path_for_month(month_key)
            conn = sqlite3.connect(month_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.executescript(
                """
                CREATE TABLE IF NOT EXISTS shipments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracking_number TEXT NOT NULL,
                    company_id INTEGER,
                    company_name TEXT NOT NULL,
                    operator_name TEXT NOT NULL DEFAULT '',
                    shipped_at TEXT NOT NULL,
                    shipping_day TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS unrecognized_shipments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shipment_id INTEGER NOT NULL UNIQUE,
                    tracking_number TEXT NOT NULL,
                    operator_name TEXT NOT NULL DEFAULT '',
                    scanned_at TEXT NOT NULL,
                    resolved_company_name TEXT,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS duplicate_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracking_number TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    operator_name TEXT NOT NULL DEFAULT '',
                    duplicate_at TEXT NOT NULL,
                    duplicate_day TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    reason_note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS anomaly_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracking_number TEXT NOT NULL,
                    operator_name TEXT NOT NULL DEFAULT '',
                    anomaly_type TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    event_day TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shipments_tracking_number ON shipments (tracking_number);
                CREATE INDEX IF NOT EXISTS idx_shipments_shipping_day ON shipments (shipping_day);
                CREATE INDEX IF NOT EXISTS idx_unrecognized_resolved_at ON unrecognized_shipments (resolved_at);
                CREATE INDEX IF NOT EXISTS idx_duplicate_events_duplicate_day ON duplicate_events (duplicate_day);
                CREATE INDEX IF NOT EXISTS idx_anomaly_events_event_day ON anomaly_events (event_day);
                """
            )
            conn.commit()
            month_connections[month_key] = conn
            return conn

        table_month_map = {
            "shipments": "shipping_day",
            "unrecognized_shipments": "scanned_at",
            "duplicate_events": "duplicate_day",
            "anomaly_events": "event_day",
        }
        for table_name, month_field in table_month_map.items():
            table_exists = source_conn.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'"
            ).fetchone()
            if not table_exists:
                continue
            rows = source_conn.execute(f"SELECT * FROM {table_name}").fetchall()
            for row in rows:
                month_key = month_key_from_date(row[month_field])
                target_conn = ensure_month_connection(month_key)
                column_names = row.keys()
                placeholders = ", ".join("?" for _ in column_names)
                columns_sql = ", ".join(column_names)
                target_conn.execute(
                    f"INSERT OR REPLACE INTO {table_name} ({columns_sql}) VALUES ({placeholders})",
                    tuple(row[column] for column in column_names),
                )
            for conn in month_connections.values():
                conn.commit()
    finally:
        source_conn.close()
        for conn in locals().get("month_connections", {}).values():
            conn.close()


def ensure_storage_ready() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_database()
    migrate_legacy_exports()


def prune_auto_backups(limit: int = MAX_AUTO_BACKUPS) -> None:
    backups = sorted(BACKUP_DIR.glob(f"{AUTO_BACKUP_PREFIX}*.zip"))
    if len(backups) <= limit:
        return
    for backup_path in backups[: len(backups) - limit]:
        try:
            backup_path.unlink()
        except OSError:
            continue

LANGUAGES = {
    "zh": "中文",
    "en": "English",
    "id": "Bahasa Indonesia",
}
DEFAULT_LANGUAGE_CODE = "id"


def normalize_language_code(value: str | None) -> str:
    code = (value or "").strip().lower()
    return code if code in LANGUAGES else DEFAULT_LANGUAGE_CODE

TRANSLATIONS = {
    "zh": {
        "app_title": "快递扫描发货管理系统",
        "language": "语言",
        "app_version": "版本",
        "scan_tab": "扫描录入",
        "stats_tab": "统计页面",
        "search_tab": "单号查询",
        "rules_tab": "快递规则",
        "unrecognized_tab": "未识别单号",
        "duplicates_tab": "重复记录",
        "scan_title": "扫描包裹条码",
        "scan_desc": "扫描枪输入快递单号后按回车，系统会自动识别快递公司并检查重复单号。",
        "scan_frame": "录入区域",
        "tracking_number": "快递单号:",
        "operator_label": "操作员:",
        "operator_quick_label": "快捷操作员:",
        "save_operator_shortcut": "保存快捷操作员",
        "quick_block_label": "包裹拦截:",
        "quick_block_hint": "可一次输入多个单号，支持换行、空格、逗号分隔。保存后扫描到会立即拦截报警。",
        "quick_block_blacklist_button": "加入黑名单",
        "quick_block_lock_button": "加入锁定",
        "quick_block_empty": "请先输入需要拦截的单号。",
        "quick_block_saved_blacklist": "已加入黑名单，共 {count} 个单号。",
        "quick_block_saved_locked": "已加入锁定，共 {count} 个单号。",
        "quick_block_recent_title": "最近加入的拦截单号",
        "duplicate_policy_label": "重复规则:",
        "sound_enabled_label": "提示音:",
        "block_unrecognized_label": "拦截未设定快递公司",
        "scan_button": "确认录入",
        "waiting_scan": "等待扫描...",
        "recent_records": "最近扫描记录",
        "company_name": "快递公司",
        "company_color_label": "公司颜色:",
        "shipped_at": "发货时间",
        "today_counts": "今日快递公司数量",
        "today_total": "今日总包裹数: {total}",
        "today_total_big_label": "今日扫描总数",
        "hourly_total_label": "本小时扫描数量",
        "duplicate_today_total_label": "今日重复单号数量",
        "quantity": "数量",
        "action": "操作",
        "delete": "删除",
        "remove": "移除",
        "stats_title": "发货统计",
        "refresh_stats": "刷新统计",
        "export_excel": "导出 Excel 报表",
        "daily_report_title": "日报表导出",
        "report_date_label": "报表日期:",
        "set_today": "今天",
        "set_yesterday": "昨天",
        "export_today_report": "导出今日报表",
        "export_selected_date_report": "导出指定日期报表",
        "start_date_label": "开始日期:",
        "end_date_label": "结束日期:",
        "company_filter_label": "快递公司筛选:",
        "stats_month_label": "月份数据库:",
        "apply_filter": "应用筛选",
        "reset_filter": "重置筛选",
        "all_companies": "全部快递公司",
        "all_months": "全部月份",
        "daily_stats": "每日包裹数量",
        "date": "日期",
        "package_total": "包裹数量",
        "company_totals": "各快递公司数量",
        "operator_totals": "操作员统计",
        "cumulative_total": "累计数量",
        "search_title": "单号查询",
        "search_frame": "查询条件",
        "search_button": "查询",
        "search_month_label": "查询月份:",
        "search_hint": "输入单号后可按月份或全部月份查询发货时间。",
        "search_results": "查询结果",
        "shipping_day": "发货日期",
        "duplicate_records_title": "今日重复记录",
        "duplicate_records_desc": "这里显示今天被系统拦截的重复单号扫描记录。",
        "duplicate_records_count": "今日重复次数: {total}",
        "last_seen_time": "上次出现时间",
        "duplicate_reason": "重复原因",
        "save_duplicate_reason": "保存备注",
        "duplicate_reason_saved": "重复备注已保存。",
        "duplicate_reason_empty": "请先输入重复备注。",
        "duplicate_select_record": "请先选择一条重复记录。",
        "throughput_10min": "最近10分钟扫描量",
        "throughput_1hour": "最近1小时扫描量",
        "throughput_avg": "平均每分钟",
        "anomaly_title": "异常扫描提醒",
        "anomaly_result": "异常扫描未保存: {tracking} | 类型: {type}",
        "anomaly_message": "检测到异常单号: {tracking}\n异常类型: {type}\n备注: {notes}\n本次扫描不会保存到数据库。",
        "anomaly_blacklist": "黑名单",
        "anomaly_locked": "锁定单号",
        "anomaly_format": "格式异常",
        "anomaly_unrecognized": "未识别",
        "intercept_blacklist_title": "禁止出货警告",
        "intercept_blacklist_message": "检测到拦截单号: {tracking}\n处理状态: 禁止出货\n备注: {notes}\n此单号已在黑名单中，本次扫描不会保存到数据库。",
        "intercept_blacklist_result": "已拦截黑名单单号: {tracking} | 禁止出货",
        "intercept_blacklist_big": "禁止出货",
        "intercept_locked_title": "暂停出货提醒",
        "intercept_locked_message": "检测到锁定单号: {tracking}\n处理状态: 暂停出货，待确认\n备注: {notes}\n请人工确认后再决定是否继续处理。",
        "intercept_locked_result": "已拦截锁定单号: {tracking} | 暂停出货，待确认",
        "intercept_locked_big": "暂停出货",
        "anomalies_tab": "异常记录",
        "anomalies_title": "异常记录",
        "anomalies_desc": "这里显示未识别、格式异常、黑名单、锁定单号等异常扫描记录。",
        "anomalies_count": "异常数量: {total}",
        "anomaly_type": "异常类型",
        "notes": "备注",
        "note_tracking_too_short": "单号长度少于 6 位",
        "no_notes": "无",
        "blacklist_tab": "黑名单/锁定",
        "blacklist_title": "黑名单与锁定单号",
        "blacklist_desc": "被加入黑名单或锁定的单号会在扫描时直接归类为异常。",
        "entry_type": "类型",
        "blacklist_type": "黑名单",
        "lock_type": "锁定",
        "save_blocked_tracking": "保存名单",
        "delete_blocked_tracking": "删除选中名单",
        "blocked_tracking_empty": "单号和类型不能为空。",
        "blocked_tracking_saved": "异常名单已保存: {tracking}",
        "blocked_tracking_deleted": "异常名单已删除。",
        "blocked_select_delete": "请先选择要删除的异常名单。",
        "archive_tab": "数据归档",
        "archive_title": "数据归档",
        "archive_desc": "把截止某日期之前的旧数据归档到独立数据库文件，减轻当前库压力。",
        "archive_before_date": "归档截止日期:",
        "archive_button": "开始归档",
        "archive_success": "归档成功",
        "archive_success_message": "归档文件已生成:\n{path}",
        "archive_failed": "归档失败",
        "archive_failed_message": "归档失败:\n{error}",
        "archive_no_rows": "没有符合条件的旧数据需要归档。",
        "archive_confirm": "归档会把旧数据移动到独立归档文件中，是否继续？",
        "rules_title": "快递公司规则",
        "rules_editor": "新增 / 编辑规则",
        "company_name_label": "快递公司名称:",
        "prefix_label": "单号前缀:",
        "rules_helper": "例如:\n- 顺丰可以同时配置 SF 和 SFW\n- 京东可以同时配置 JD 和 JDX\n- 同一个快递公司允许配置多个单号前缀",
        "save_rule": "保存规则",
        "choose_color": "选择颜色",
        "test_rule_label": "规则测试单号:",
        "test_rule_button": "测试规则",
        "test_rule_result_default": "输入单号后，可立即看到会匹配到哪个公司和颜色。",
        "test_rule_result": "匹配结果: {company} | 颜色: {color}",
        "clear_form": "清空输入",
        "delete_rule": "删除选中规则",
        "rules_status_default": "可维护快递单号前缀和公司映射关系，同一快递公司可添加多个规则。",
        "unrecognized_title": "未识别单号专区",
        "unrecognized_desc": "这里显示还没有匹配到快递规则的单号。新增规则后系统会自动重新识别。",
        "unrecognized_count": "未识别数量: {total}",
        "warning": "提示",
        "scan_empty": "请先扫描或输入快递单号。",
        "duplicate_all": "全部历史",
        "duplicate_today": "仅当天",
        "sound_on": "开启",
        "sound_off": "关闭",
        "duplicate_title": "重复单号警告",
        "duplicate_message": "检测到重复单号: {tracking}\n上一次出现时间: {time}\n上一次快递公司: {company}\n本次扫描不会保存到数据库。",
        "duplicate_result": "重复单号未保存: {tracking} | 上次时间: {time}",
        "operator_shortcut_saved": "已保存快捷操作员: {name}",
        "operator_shortcut_empty": "请先输入操作员名称再保存。",
        "operator_shortcut_delete_confirm": "确定要移除这个快捷操作员吗？",
        "save_success": "录入成功: {tracking} | 快递公司: {company} | 时间: {time}",
        "big_company_default": "等待识别快递公司",
        "export_failed": "导出失败",
        "export_failed_message": "报表写入失败:\n{error}",
        "export_success": "导出成功",
        "export_success_message": "汇总报表已导出到:\n{summary_path}\n\n明细报表已导出到:\n{detail_path}",
        "export_sheet_filtered_companies": "筛选公司数量",
        "export_sheet_daily_stats": "每日数量",
        "export_sheet_company_stats": "公司统计",
        "export_sheet_operator_stats": "操作员统计",
        "export_sheet_shipments": "发货明细",
        "export_sheet_unrecognized": "未识别单号",
        "export_color": "颜色",
        "export_scanned_at": "扫描时间",
        "backup_data": "备份数据",
        "restore_data": "恢复数据",
        "check_update": "检查更新",
        "update_not_supported": "在线升级只支持 Windows 打包后的 exe 版本。",
        "update_manifest_missing": "请先在“设置”中配置升级清单地址。",
        "update_available_title": "发现新版本",
        "update_available_message": "当前版本: {current}\n最新版本: {latest}\n是否现在下载并升级？",
        "update_latest": "当前已经是最新版本。",
        "update_check_failed": "检查更新失败:\n{error}",
        "update_download_failed": "下载更新失败:\n{error}",
        "update_invalid_manifest": "升级清单格式不正确。",
        "update_started": "更新程序已启动，主程序即将关闭并升级。",
        "update_backup_failed": "升级前备份失败:\n{error}",
        "update_settings_title": "在线升级设置",
        "update_manifest_label": "升级清单地址:",
        "update_settings_saved": "升级清单地址已保存。",
        "update_settings_button": "升级设置",
        "backup_success": "备份成功",
        "backup_success_message": "数据库备份已保存到:\n{path}",
        "backup_failed": "备份失败",
        "backup_failed_message": "数据库备份失败:\n{error}",
        "backup_management": "备份管理",
        "backup_file": "备份文件",
        "backup_created_at": "创建时间",
        "backup_size": "文件大小",
        "backup_type": "备份类型",
        "backup_search_label": "搜索备份:",
        "backup_search_hint": "输入文件名、类型或时间",
        "backup_search_clear": "清空搜索",
        "backup_refresh": "刷新备份列表",
        "backup_open_folder": "打开备份文件夹",
        "backup_sort_desc": "时间排序: 最新在前",
        "backup_sort_asc": "时间排序: 最旧在前",
        "backup_restore_selected": "恢复选中备份",
        "backup_delete_selected": "删除选中备份",
        "backup_type_auto": "自动备份",
        "backup_type_manual": "手动备份",
        "backup_type_pre_restore": "恢复前备份",
        "backup_type_other": "其他备份",
        "backup_no_selection": "请先选择一个备份文件。",
        "backup_delete_confirm_title": "确认删除备份",
        "backup_delete_confirm_message": "确定要删除选中的备份文件吗？此操作不可撤销。",
        "backup_delete_success": "备份已删除: {path}",
        "backup_delete_failed": "删除备份失败:\n{error}",
        "backup_open_folder_failed": "打开备份文件夹失败:\n{error}",
        "backup_no_results": "没有匹配的备份文件。",
        "restore_confirm_title": "确认恢复",
        "restore_confirm_message": "恢复备份会覆盖当前数据库。\n程序会先自动备份当前数据。\n是否继续？",
        "restore_no_backup": "备份目录中还没有可恢复的数据库备份文件。",
        "restore_select_title": "选择要恢复的备份文件",
        "restore_success": "恢复成功",
        "restore_success_message": "数据库已从以下备份恢复:\n{path}",
        "restore_failed": "恢复失败",
        "restore_failed_message": "数据库恢复失败:\n{error}",
        "cancel": "取消",
        "search_empty": "请输入需要查询的快递单号。",
        "search_not_found": "未找到单号 {tracking} 的发货记录。",
        "search_found": "共找到 {count} 条记录，最新发货时间: {time}",
        "invalid_date": "日期格式应为 YYYY-MM-DD，请检查后重试。",
        "rule_cleared": "已清空输入，可新增规则。",
        "rule_empty": "快递公司名称和单号前缀都不能为空。",
        "color_empty": "请设置快递公司的显示颜色。",
        "rule_save_failed": "保存失败",
        "rule_save_failed_message": "单号前缀已存在，请检查后重试。同一个快递公司可以使用多个不同前缀。",
        "rule_saved": "规则已保存: {name} -> {prefix}",
        "rule_saved_reprocessed": "规则已保存: {name} -> {prefix}，并自动重新识别了 {count} 条未识别单号。",
        "rule_select_delete": "请先在左侧选择要删除的规则。",
        "delete_record_title": "删除记录",
        "delete_record_message": "确定要删除这个最近扫描记录吗？",
        "record_deleted": "扫描记录已删除。",
        "delete_confirm_title": "确认删除",
        "delete_confirm_message": "删除后将不再识别该规则，是否继续？",
        "rule_deleted": "规则已删除。",
        "unrecognized": "未识别",
        "id": "ID",
    },
    "en": {
        "app_title": "Courier Scan Shipping Manager",
        "language": "Language",
        "app_version": "Version",
        "scan_tab": "Scanning",
        "stats_tab": "Statistics",
        "search_tab": "Tracking Search",
        "rules_tab": "Courier Rules",
        "unrecognized_tab": "Unrecognized",
        "duplicates_tab": "Duplicates",
        "scan_title": "Scan Package Barcode",
        "scan_desc": "Scan or enter the tracking number and press Enter. The system will identify the courier and check for duplicates.",
        "scan_frame": "Entry Area",
        "tracking_number": "Tracking Number:",
        "operator_label": "Operator:",
        "operator_quick_label": "Quick Operators:",
        "save_operator_shortcut": "Save Quick Operator",
        "quick_block_label": "Parcel Intercept:",
        "quick_block_hint": "You can enter multiple tracking numbers at once. Line breaks, spaces, and commas are supported.",
        "quick_block_blacklist_button": "Add to Blacklist",
        "quick_block_lock_button": "Add to Locked List",
        "quick_block_empty": "Please enter at least one tracking number to intercept.",
        "quick_block_saved_blacklist": "{count} tracking number(s) added to the blacklist.",
        "quick_block_saved_locked": "{count} tracking number(s) added to the locked list.",
        "quick_block_recent_title": "Recently Added Intercepts",
        "duplicate_policy_label": "Duplicate Rule:",
        "sound_enabled_label": "Sound:",
        "block_unrecognized_label": "Intercept unconfigured couriers",
        "scan_button": "Save Scan",
        "waiting_scan": "Waiting for scan...",
        "recent_records": "Recent Scan Records",
        "company_name": "Courier",
        "company_color_label": "Company Color:",
        "shipped_at": "Shipped Time",
        "today_counts": "Today's Courier Counts",
        "today_total": "Today's Total Packages: {total}",
        "today_total_big_label": "Today's Scan Total",
        "hourly_total_label": "Scans This Hour",
        "duplicate_today_total_label": "Today's Duplicate Count",
        "quantity": "Quantity",
        "action": "Action",
        "delete": "Delete",
        "remove": "Remove",
        "stats_title": "Shipping Statistics",
        "refresh_stats": "Refresh",
        "export_excel": "Export Excel Report",
        "daily_report_title": "Daily Report Export",
        "report_date_label": "Report Date:",
        "set_today": "Today",
        "set_yesterday": "Yesterday",
        "export_today_report": "Export Today's Report",
        "export_selected_date_report": "Export Selected Date",
        "start_date_label": "Start Date:",
        "end_date_label": "End Date:",
        "company_filter_label": "Courier Filter:",
        "stats_month_label": "Month Database:",
        "apply_filter": "Apply Filter",
        "reset_filter": "Reset Filter",
        "all_companies": "All Couriers",
        "all_months": "All Months",
        "daily_stats": "Daily Package Totals",
        "date": "Date",
        "package_total": "Packages",
        "company_totals": "Courier Totals",
        "operator_totals": "Operator Totals",
        "cumulative_total": "Total",
        "search_title": "Tracking Number Search",
        "search_frame": "Search Criteria",
        "search_button": "Search",
        "search_month_label": "Search Month:",
        "search_hint": "Enter a tracking number and search one month or all months.",
        "search_results": "Search Results",
        "shipping_day": "Shipping Date",
        "duplicate_records_title": "Today's Duplicate Records",
        "duplicate_records_desc": "This area shows duplicate tracking number scans blocked by the system today.",
        "duplicate_records_count": "Today's duplicate events: {total}",
        "last_seen_time": "Last Seen Time",
        "duplicate_reason": "Duplicate Reason",
        "save_duplicate_reason": "Save Note",
        "duplicate_reason_saved": "Duplicate note saved.",
        "duplicate_reason_empty": "Please enter a duplicate note first.",
        "duplicate_select_record": "Please select a duplicate record first.",
        "throughput_10min": "Scans in Last 10 Minutes",
        "throughput_1hour": "Scans in Last 1 Hour",
        "throughput_avg": "Avg per Minute",
        "anomaly_title": "Anomaly Scan Warning",
        "anomaly_result": "Anomaly not saved: {tracking} | Type: {type}",
        "anomaly_message": "An anomalous tracking number was detected: {tracking}\nType: {type}\nNotes: {notes}\nThis scan will not be saved.",
        "anomaly_blacklist": "Blacklisted",
        "anomaly_locked": "Locked",
        "anomaly_format": "Invalid Format",
        "anomaly_unrecognized": "Unrecognized",
        "intercept_blacklist_title": "Shipment Blocked",
        "intercept_blacklist_message": "Intercepted tracking number: {tracking}\nStatus: Shipment blocked\nNotes: {notes}\nThis tracking number is blacklisted and will not be saved.",
        "intercept_blacklist_result": "Blacklisted tracking intercepted: {tracking} | Shipment blocked",
        "intercept_blacklist_big": "Shipment Blocked",
        "intercept_locked_title": "Shipment On Hold",
        "intercept_locked_message": "Locked tracking number detected: {tracking}\nStatus: On hold, pending confirmation\nNotes: {notes}\nPlease confirm manually before processing this shipment.",
        "intercept_locked_result": "Locked tracking intercepted: {tracking} | Pending confirmation",
        "intercept_locked_big": "On Hold",
        "anomalies_tab": "Anomalies",
        "anomalies_title": "Anomaly Records",
        "anomalies_desc": "This area shows unrecognized, invalid-format, blacklisted, locked, and other anomaly scans.",
        "anomalies_count": "Anomaly count: {total}",
        "anomaly_type": "Anomaly Type",
        "notes": "Notes",
        "note_tracking_too_short": "Tracking number is shorter than 6 characters",
        "no_notes": "None",
        "blacklist_tab": "Blacklist/Locked",
        "blacklist_title": "Blacklisted and Locked Tracking Numbers",
        "blacklist_desc": "Tracking numbers in this list will be classified as anomalies immediately when scanned.",
        "entry_type": "Type",
        "blacklist_type": "Blacklist",
        "lock_type": "Locked",
        "save_blocked_tracking": "Save Entry",
        "delete_blocked_tracking": "Delete Selected Entry",
        "blocked_tracking_empty": "Tracking number and type cannot be empty.",
        "blocked_tracking_saved": "Blocked entry saved: {tracking}",
        "blocked_tracking_deleted": "Blocked entry deleted.",
        "blocked_select_delete": "Please select a blocked entry to delete.",
        "archive_tab": "Archive",
        "archive_title": "Data Archive",
        "archive_desc": "Move old data before a cutoff date into a separate archive database file to keep the active database smaller.",
        "archive_before_date": "Archive Before Date:",
        "archive_button": "Start Archive",
        "archive_success": "Archive Successful",
        "archive_success_message": "Archive file created:\n{path}",
        "archive_failed": "Archive Failed",
        "archive_failed_message": "Archive failed:\n{error}",
        "archive_no_rows": "There is no old data matching the archive criteria.",
        "archive_confirm": "Archiving will move old data into a separate archive file. Continue?",
        "rules_title": "Courier Company Rules",
        "rules_editor": "Add / Edit Rule",
        "company_name_label": "Courier Name:",
        "prefix_label": "Tracking Prefix:",
        "rules_helper": "Examples:\n- SF Express can use both SF and SFW\n- JD Logistics can use both JD and JDX\n- One courier company can have multiple tracking prefixes",
        "save_rule": "Save Rule",
        "choose_color": "Choose Color",
        "test_rule_label": "Test Tracking Number:",
        "test_rule_button": "Test Rule",
        "test_rule_result_default": "Enter a tracking number to instantly see the matched courier and color.",
        "test_rule_result": "Match result: {company} | Color: {color}",
        "clear_form": "Clear",
        "delete_rule": "Delete Selected Rule",
        "rules_status_default": "Manage the mapping between tracking prefixes and courier companies. One courier can have multiple rules.",
        "unrecognized_title": "Unrecognized Tracking Numbers",
        "unrecognized_desc": "This area shows tracking numbers that do not match any courier rule yet. After adding a rule, the system will try to re-identify them.",
        "unrecognized_count": "Unrecognized count: {total}",
        "warning": "Notice",
        "scan_empty": "Please scan or enter a tracking number first.",
        "duplicate_all": "All History",
        "duplicate_today": "Today Only",
        "sound_on": "On",
        "sound_off": "Off",
        "duplicate_title": "Duplicate Tracking Number",
        "duplicate_message": "Duplicate tracking number detected: {tracking}\nLast seen: {time}\nLast courier: {company}\nThis scan will not be saved.",
        "duplicate_result": "Duplicate not saved: {tracking} | Last seen: {time}",
        "operator_shortcut_saved": "Quick operator saved: {name}",
        "operator_shortcut_empty": "Please enter an operator name before saving it.",
        "operator_shortcut_delete_confirm": "Are you sure you want to remove this quick operator?",
        "save_success": "Saved: {tracking} | Courier: {company} | Time: {time}",
        "big_company_default": "Waiting to identify courier",
        "export_failed": "Export Failed",
        "export_failed_message": "Failed to write report:\n{error}",
        "export_success": "Export Successful",
        "export_success_message": "Summary report exported to:\n{summary_path}\n\nDetail report exported to:\n{detail_path}",
        "export_sheet_filtered_companies": "Filtered Courier Totals",
        "export_sheet_daily_stats": "Daily Totals",
        "export_sheet_company_stats": "Courier Statistics",
        "export_sheet_operator_stats": "Operator Statistics",
        "export_sheet_shipments": "Shipment Details",
        "export_sheet_unrecognized": "Unrecognized Tracking Numbers",
        "export_color": "Color",
        "export_scanned_at": "Scanned Time",
        "backup_data": "Backup Data",
        "restore_data": "Restore Data",
        "check_update": "Check for Updates",
        "update_not_supported": "Online updates are supported only for the packaged Windows exe version.",
        "update_manifest_missing": "Please configure the update manifest URL in Settings first.",
        "update_available_title": "Update Available",
        "update_available_message": "Current version: {current}\nLatest version: {latest}\nDownload and install now?",
        "update_latest": "You are already using the latest version.",
        "update_check_failed": "Failed to check updates:\n{error}",
        "update_download_failed": "Failed to download update:\n{error}",
        "update_invalid_manifest": "The update manifest format is invalid.",
        "update_started": "The updater has started. The app will now close and install the update.",
        "update_backup_failed": "Failed to create a pre-update backup:\n{error}",
        "update_settings_title": "Online Update Settings",
        "update_manifest_label": "Update Manifest URL:",
        "update_settings_saved": "Update manifest URL saved.",
        "update_settings_button": "Update Settings",
        "backup_success": "Backup Successful",
        "backup_success_message": "Database backup saved to:\n{path}",
        "backup_failed": "Backup Failed",
        "backup_failed_message": "Failed to back up database:\n{error}",
        "backup_management": "Backup Management",
        "backup_file": "Backup File",
        "backup_created_at": "Created At",
        "backup_size": "File Size",
        "backup_type": "Backup Type",
        "backup_search_label": "Search Backups:",
        "backup_search_hint": "Search by file, type, or time",
        "backup_search_clear": "Clear Search",
        "backup_refresh": "Refresh Backups",
        "backup_open_folder": "Open Backup Folder",
        "backup_sort_desc": "Sort by Time: Newest First",
        "backup_sort_asc": "Sort by Time: Oldest First",
        "backup_restore_selected": "Restore Selected Backup",
        "backup_delete_selected": "Delete Selected Backup",
        "backup_type_auto": "Auto Backup",
        "backup_type_manual": "Manual Backup",
        "backup_type_pre_restore": "Pre-Restore Backup",
        "backup_type_other": "Other Backup",
        "backup_no_selection": "Please select a backup file first.",
        "backup_delete_confirm_title": "Confirm Backup Deletion",
        "backup_delete_confirm_message": "Are you sure you want to delete the selected backup file? This cannot be undone.",
        "backup_delete_success": "Backup deleted: {path}",
        "backup_delete_failed": "Failed to delete backup:\n{error}",
        "backup_open_folder_failed": "Failed to open backup folder:\n{error}",
        "backup_no_results": "No backup files match the current search.",
        "restore_confirm_title": "Confirm Restore",
        "restore_confirm_message": "Restoring a backup will overwrite the current database.\nThe app will first create an automatic safety backup.\nContinue?",
        "restore_no_backup": "There are no database backup files available in the backup folder yet.",
        "restore_select_title": "Select a backup file to restore",
        "restore_success": "Restore Successful",
        "restore_success_message": "Database restored from:\n{path}",
        "restore_failed": "Restore Failed",
        "restore_failed_message": "Failed to restore database:\n{error}",
        "cancel": "Cancel",
        "search_empty": "Please enter a tracking number to search.",
        "search_not_found": "No shipping record found for {tracking}.",
        "search_found": "{count} record(s) found. Latest shipping time: {time}",
        "invalid_date": "Date format must be YYYY-MM-DD. Please check and try again.",
        "rule_cleared": "The form has been cleared. You can add a new rule now.",
        "rule_empty": "Courier name and tracking prefix cannot be empty.",
        "color_empty": "Please set a display color for the courier.",
        "rule_save_failed": "Save Failed",
        "rule_save_failed_message": "This tracking prefix already exists. Please check and try again. One courier can use multiple different prefixes.",
        "rule_saved": "Rule saved: {name} -> {prefix}",
        "rule_saved_reprocessed": "Rule saved: {name} -> {prefix}. The system re-identified {count} unrecognized tracking number(s).",
        "rule_select_delete": "Please select a rule on the left before deleting.",
        "delete_record_title": "Delete Record",
        "delete_record_message": "Are you sure you want to delete this recent scan record?",
        "record_deleted": "Scan record deleted.",
        "delete_confirm_title": "Confirm Delete",
        "delete_confirm_message": "This rule will no longer be used for recognition. Continue?",
        "rule_deleted": "Rule deleted.",
        "unrecognized": "Unrecognized",
        "id": "ID",
    },
    "id": {
        "app_title": "Manajer Pengiriman dan Scan Resi",
        "language": "Bahasa",
        "app_version": "Versi",
        "scan_tab": "Pemindaian",
        "stats_tab": "Statistik",
        "search_tab": "Cari Resi",
        "rules_tab": "Aturan Kurir",
        "unrecognized_tab": "Tidak Dikenali",
        "duplicates_tab": "Duplikat",
        "scan_title": "Pindai Barcode Paket",
        "scan_desc": "Pindai atau masukkan nomor resi lalu tekan Enter. Sistem akan mengenali kurir dan memeriksa duplikasi.",
        "scan_frame": "Area Input",
        "tracking_number": "Nomor Resi:",
        "operator_label": "Operator:",
        "operator_quick_label": "Operator Cepat:",
        "save_operator_shortcut": "Simpan Operator Cepat",
        "quick_block_label": "Intersep Paket:",
        "quick_block_hint": "Bisa memasukkan banyak nomor resi sekaligus. Pisahkan dengan baris baru, spasi, atau koma.",
        "quick_block_blacklist_button": "Tambahkan ke Blacklist",
        "quick_block_lock_button": "Tambahkan ke Kunci",
        "quick_block_empty": "Masukkan minimal satu nomor resi yang akan diintersep.",
        "quick_block_saved_blacklist": "{count} nomor resi ditambahkan ke blacklist.",
        "quick_block_saved_locked": "{count} nomor resi ditambahkan ke daftar kunci.",
        "quick_block_recent_title": "Nomor Intersep Terbaru",
        "duplicate_policy_label": "Aturan Duplikat:",
        "sound_enabled_label": "Suara:",
        "block_unrecognized_label": "Intersep kurir yang belum diatur",
        "scan_button": "Simpan Scan",
        "waiting_scan": "Menunggu pemindaian...",
        "recent_records": "Riwayat Pemindaian Terbaru",
        "company_name": "Kurir",
        "company_color_label": "Warna Kurir:",
        "shipped_at": "Waktu Kirim",
        "today_counts": "Jumlah Kurir Hari Ini",
        "today_total": "Total Paket Hari Ini: {total}",
        "today_total_big_label": "Total Scan Hari Ini",
        "hourly_total_label": "Jumlah Scan Jam Ini",
        "duplicate_today_total_label": "Jumlah Duplikat Hari Ini",
        "quantity": "Jumlah",
        "action": "Aksi",
        "delete": "Hapus",
        "remove": "Hapus Cepat",
        "stats_title": "Statistik Pengiriman",
        "refresh_stats": "Muat Ulang",
        "export_excel": "Ekspor Laporan Excel",
        "daily_report_title": "Ekspor Laporan Harian",
        "report_date_label": "Tanggal Laporan:",
        "set_today": "Hari Ini",
        "set_yesterday": "Kemarin",
        "export_today_report": "Ekspor Hari Ini",
        "export_selected_date_report": "Ekspor Tanggal Pilihan",
        "start_date_label": "Tanggal Mulai:",
        "end_date_label": "Tanggal Akhir:",
        "company_filter_label": "Filter Kurir:",
        "stats_month_label": "Database Bulan:",
        "apply_filter": "Terapkan Filter",
        "reset_filter": "Reset Filter",
        "all_companies": "Semua Kurir",
        "all_months": "Semua Bulan",
        "daily_stats": "Total Paket Harian",
        "date": "Tanggal",
        "package_total": "Jumlah Paket",
        "company_totals": "Total per Kurir",
        "operator_totals": "Statistik Operator",
        "cumulative_total": "Total",
        "search_title": "Pencarian Nomor Resi",
        "search_frame": "Kriteria Pencarian",
        "search_button": "Cari",
        "search_month_label": "Bulan Pencarian:",
        "search_hint": "Masukkan nomor resi lalu cari per bulan atau semua bulan.",
        "search_results": "Hasil Pencarian",
        "shipping_day": "Tanggal Kirim",
        "duplicate_records_title": "Catatan Duplikat Hari Ini",
        "duplicate_records_desc": "Area ini menampilkan scan nomor resi duplikat yang diblokir sistem hari ini.",
        "duplicate_records_count": "Jumlah duplikat hari ini: {total}",
        "last_seen_time": "Waktu Terakhir",
        "duplicate_reason": "Alasan Duplikat",
        "save_duplicate_reason": "Simpan Catatan",
        "duplicate_reason_saved": "Catatan duplikat disimpan.",
        "duplicate_reason_empty": "Masukkan catatan duplikat terlebih dahulu.",
        "duplicate_select_record": "Pilih satu catatan duplikat terlebih dahulu.",
        "throughput_10min": "Scan 10 Menit Terakhir",
        "throughput_1hour": "Scan 1 Jam Terakhir",
        "throughput_avg": "Rata-rata per Menit",
        "anomaly_title": "Peringatan Anomali",
        "anomaly_result": "Anomali tidak disimpan: {tracking} | Jenis: {type}",
        "anomaly_message": "Nomor resi anomali terdeteksi: {tracking}\nJenis: {type}\nCatatan: {notes}\nPemindaian ini tidak akan disimpan.",
        "anomaly_blacklist": "Blacklist",
        "anomaly_locked": "Terkunci",
        "anomaly_format": "Format Tidak Valid",
        "anomaly_unrecognized": "Tidak Dikenali",
        "intercept_blacklist_title": "Pengiriman Diblokir",
        "intercept_blacklist_message": "Nomor resi intersep terdeteksi: {tracking}\nStatus: Dilarang kirim\nCatatan: {notes}\nNomor ini ada di blacklist dan tidak akan disimpan.",
        "intercept_blacklist_result": "Nomor blacklist diintersep: {tracking} | Dilarang kirim",
        "intercept_blacklist_big": "Dilarang Kirim",
        "intercept_locked_title": "Pengiriman Ditahan",
        "intercept_locked_message": "Nomor resi terkunci terdeteksi: {tracking}\nStatus: Tahan dulu, menunggu konfirmasi\nCatatan: {notes}\nSilakan konfirmasi manual sebelum melanjutkan.",
        "intercept_locked_result": "Nomor terkunci diintersep: {tracking} | Menunggu konfirmasi",
        "intercept_locked_big": "Tahan Dulu",
        "anomalies_tab": "Anomali",
        "anomalies_title": "Catatan Anomali",
        "anomalies_desc": "Area ini menampilkan scan yang tidak dikenali, format tidak valid, blacklist, lock, dan anomali lain.",
        "anomalies_count": "Jumlah anomali: {total}",
        "anomaly_type": "Jenis Anomali",
        "notes": "Catatan",
        "note_tracking_too_short": "Nomor resi kurang dari 6 karakter",
        "no_notes": "Tidak ada",
        "blacklist_tab": "Blacklist/Kunci",
        "blacklist_title": "Nomor Resi Blacklist dan Terkunci",
        "blacklist_desc": "Nomor resi dalam daftar ini akan langsung diklasifikasikan sebagai anomali saat dipindai.",
        "entry_type": "Jenis",
        "blacklist_type": "Blacklist",
        "lock_type": "Terkunci",
        "save_blocked_tracking": "Simpan Entri",
        "delete_blocked_tracking": "Hapus Entri Terpilih",
        "blocked_tracking_empty": "Nomor resi dan jenis tidak boleh kosong.",
        "blocked_tracking_saved": "Entri anomali disimpan: {tracking}",
        "blocked_tracking_deleted": "Entri anomali dihapus.",
        "blocked_select_delete": "Pilih entri anomali yang akan dihapus terlebih dahulu.",
        "archive_tab": "Arsip",
        "archive_title": "Arsip Data",
        "archive_desc": "Pindahkan data lama sebelum tanggal tertentu ke file database arsip terpisah agar database aktif tetap ringan.",
        "archive_before_date": "Arsip Sebelum Tanggal:",
        "archive_button": "Mulai Arsip",
        "archive_success": "Arsip Berhasil",
        "archive_success_message": "File arsip dibuat:\n{path}",
        "archive_failed": "Arsip Gagal",
        "archive_failed_message": "Arsip gagal:\n{error}",
        "archive_no_rows": "Tidak ada data lama yang cocok untuk diarsipkan.",
        "archive_confirm": "Pengarsipan akan memindahkan data lama ke file arsip terpisah. Lanjutkan?",
        "rules_title": "Aturan Perusahaan Kurir",
        "rules_editor": "Tambah / Edit Aturan",
        "company_name_label": "Nama Kurir:",
        "prefix_label": "Prefix Resi:",
        "rules_helper": "Contoh:\n- SF Express bisa memakai SF dan SFW\n- JD Logistics bisa memakai JD dan JDX\n- Satu perusahaan kurir boleh memiliki beberapa prefix resi",
        "save_rule": "Simpan Aturan",
        "choose_color": "Pilih Warna",
        "test_rule_label": "Nomor Resi Uji:",
        "test_rule_button": "Uji Aturan",
        "test_rule_result_default": "Masukkan nomor resi untuk langsung melihat kurir dan warna yang akan cocok.",
        "test_rule_result": "Hasil cocok: {company} | Warna: {color}",
        "clear_form": "Kosongkan",
        "delete_rule": "Hapus Aturan Terpilih",
        "rules_status_default": "Kelola pemetaan antara prefix resi dan perusahaan kurir. Satu kurir dapat memiliki beberapa aturan.",
        "unrecognized_title": "Area Resi Tidak Dikenali",
        "unrecognized_desc": "Di sini ditampilkan nomor resi yang belum cocok dengan aturan kurir mana pun. Setelah menambah aturan, sistem akan mencoba mengenalinya lagi.",
        "unrecognized_count": "Jumlah tidak dikenali: {total}",
        "warning": "Pemberitahuan",
        "scan_empty": "Silakan pindai atau masukkan nomor resi terlebih dahulu.",
        "duplicate_all": "Semua Riwayat",
        "duplicate_today": "Hanya Hari Ini",
        "sound_on": "Aktif",
        "sound_off": "Nonaktif",
        "duplicate_title": "Resi Duplikat",
        "duplicate_message": "Nomor resi duplikat terdeteksi: {tracking}\nTerakhir muncul: {time}\nKurir terakhir: {company}\nPemindaian ini tidak akan disimpan.",
        "duplicate_result": "Duplikat tidak disimpan: {tracking} | Terakhir: {time}",
        "operator_shortcut_saved": "Operator cepat disimpan: {name}",
        "operator_shortcut_empty": "Masukkan nama operator terlebih dahulu sebelum menyimpannya.",
        "operator_shortcut_delete_confirm": "Yakin ingin menghapus operator cepat ini?",
        "save_success": "Berhasil disimpan: {tracking} | Kurir: {company} | Waktu: {time}",
        "big_company_default": "Menunggu identifikasi kurir",
        "export_failed": "Ekspor Gagal",
        "export_failed_message": "Gagal menulis laporan:\n{error}",
        "export_success": "Ekspor Berhasil",
        "export_success_message": "Laporan ringkasan diekspor ke:\n{summary_path}\n\nLaporan detail diekspor ke:\n{detail_path}",
        "export_sheet_filtered_companies": "Total Kurir Terfilter",
        "export_sheet_daily_stats": "Total Harian",
        "export_sheet_company_stats": "Statistik Kurir",
        "export_sheet_operator_stats": "Statistik Operator",
        "export_sheet_shipments": "Detail Pengiriman",
        "export_sheet_unrecognized": "Nomor Resi Tidak Dikenali",
        "export_color": "Warna",
        "export_scanned_at": "Waktu Pindai",
        "backup_data": "Cadangkan Data",
        "restore_data": "Pulihkan Data",
        "check_update": "Periksa Pembaruan",
        "update_not_supported": "Pembaruan online hanya didukung untuk versi exe Windows yang sudah dipaketkan.",
        "update_manifest_missing": "Silakan isi URL manifest pembaruan terlebih dahulu di pengaturan.",
        "update_available_title": "Pembaruan Tersedia",
        "update_available_message": "Versi saat ini: {current}\nVersi terbaru: {latest}\nUnduh dan pasang sekarang?",
        "update_latest": "Versi ini sudah yang terbaru.",
        "update_check_failed": "Gagal memeriksa pembaruan:\n{error}",
        "update_download_failed": "Gagal mengunduh pembaruan:\n{error}",
        "update_invalid_manifest": "Format manifest pembaruan tidak valid.",
        "update_started": "Program pembaruan telah dijalankan. Aplikasi akan ditutup untuk memasang pembaruan.",
        "update_backup_failed": "Gagal membuat cadangan sebelum pembaruan:\n{error}",
        "update_settings_title": "Pengaturan Pembaruan Online",
        "update_manifest_label": "URL Manifest Pembaruan:",
        "update_settings_saved": "URL manifest pembaruan disimpan.",
        "update_settings_button": "Pengaturan Update",
        "backup_success": "Pencadangan Berhasil",
        "backup_success_message": "Cadangan database disimpan di:\n{path}",
        "backup_failed": "Pencadangan Gagal",
        "backup_failed_message": "Gagal mencadangkan database:\n{error}",
        "backup_management": "Manajemen Cadangan",
        "backup_file": "File Cadangan",
        "backup_created_at": "Waktu Dibuat",
        "backup_size": "Ukuran File",
        "backup_type": "Jenis Cadangan",
        "backup_search_label": "Cari Cadangan:",
        "backup_search_hint": "Cari nama file, jenis, atau waktu",
        "backup_search_clear": "Kosongkan Pencarian",
        "backup_refresh": "Muat Ulang Cadangan",
        "backup_open_folder": "Buka Folder Cadangan",
        "backup_sort_desc": "Urut Waktu: Terbaru Dulu",
        "backup_sort_asc": "Urut Waktu: Terlama Dulu",
        "backup_restore_selected": "Pulihkan Cadangan Terpilih",
        "backup_delete_selected": "Hapus Cadangan Terpilih",
        "backup_type_auto": "Cadangan Otomatis",
        "backup_type_manual": "Cadangan Manual",
        "backup_type_pre_restore": "Cadangan Sebelum Pemulihan",
        "backup_type_other": "Cadangan Lainnya",
        "backup_no_selection": "Silakan pilih file cadangan terlebih dahulu.",
        "backup_delete_confirm_title": "Konfirmasi Hapus Cadangan",
        "backup_delete_confirm_message": "Yakin ingin menghapus file cadangan yang dipilih? Tindakan ini tidak dapat dibatalkan.",
        "backup_delete_success": "Cadangan dihapus: {path}",
        "backup_delete_failed": "Gagal menghapus cadangan:\n{error}",
        "backup_open_folder_failed": "Gagal membuka folder cadangan:\n{error}",
        "backup_no_results": "Tidak ada file cadangan yang cocok dengan pencarian saat ini.",
        "restore_confirm_title": "Konfirmasi Pemulihan",
        "restore_confirm_message": "Memulihkan cadangan akan menimpa database saat ini.\nProgram akan membuat cadangan pengaman terlebih dahulu.\nLanjutkan?",
        "restore_no_backup": "Belum ada file cadangan database yang bisa dipulihkan di folder backup.",
        "restore_select_title": "Pilih file cadangan untuk dipulihkan",
        "restore_success": "Pemulihan Berhasil",
        "restore_success_message": "Database dipulihkan dari:\n{path}",
        "restore_failed": "Pemulihan Gagal",
        "restore_failed_message": "Gagal memulihkan database:\n{error}",
        "cancel": "Batal",
        "search_empty": "Silakan masukkan nomor resi yang ingin dicari.",
        "search_not_found": "Tidak ada catatan pengiriman untuk {tracking}.",
        "search_found": "Ditemukan {count} data. Waktu pengiriman terbaru: {time}",
        "invalid_date": "Format tanggal harus YYYY-MM-DD. Silakan periksa lalu coba lagi.",
        "rule_cleared": "Form telah dikosongkan. Anda bisa menambah aturan baru.",
        "rule_empty": "Nama kurir dan prefix resi tidak boleh kosong.",
        "color_empty": "Silakan tentukan warna tampilan untuk kurir.",
        "rule_save_failed": "Gagal Menyimpan",
        "rule_save_failed_message": "Prefix resi sudah ada. Silakan periksa lalu coba lagi. Satu kurir dapat memakai beberapa prefix berbeda.",
        "rule_saved": "Aturan tersimpan: {name} -> {prefix}",
        "rule_saved_reprocessed": "Aturan tersimpan: {name} -> {prefix}. Sistem berhasil mengenali ulang {count} nomor resi yang sebelumnya tidak dikenali.",
        "rule_select_delete": "Pilih aturan di sebelah kiri terlebih dahulu sebelum menghapus.",
        "delete_record_title": "Hapus Data",
        "delete_record_message": "Yakin ingin menghapus riwayat scan ini?",
        "record_deleted": "Riwayat scan dihapus.",
        "delete_confirm_title": "Konfirmasi Hapus",
        "delete_confirm_message": "Aturan ini tidak akan dipakai lagi untuk pengenalan. Lanjutkan?",
        "rule_deleted": "Aturan dihapus.",
        "unrecognized": "Tidak Dikenali",
        "id": "ID",
    },
}


class Database:
    def __init__(self, db_path: Path, translate: Any | None = None, mode: str = "full") -> None:
        self.db_path = db_path
        self.translate = translate
        self.mode = mode
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def backup_to(self, backup_path: Path) -> Path:
        if self.conn is None:
            raise sqlite3.ProgrammingError("Database connection is closed.")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(backup_path)
        try:
            self.conn.backup(destination)
        finally:
            destination.close()
        return backup_path

    def _create_tables(self) -> None:
        cursor = self.conn.cursor()
        if self.mode in {"full", "config", "monthly"}:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    prefix TEXT NOT NULL UNIQUE,
                    color TEXT NOT NULL DEFAULT '#0B5CAB',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS blocked_tracking_numbers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracking_number TEXT NOT NULL UNIQUE,
                    entry_type TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        if self.mode in {"full", "monthly"}:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS shipments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracking_number TEXT NOT NULL,
                    company_id INTEGER,
                    company_name TEXT NOT NULL,
                    operator_name TEXT NOT NULL DEFAULT '',
                    shipped_at TEXT NOT NULL,
                    shipping_day TEXT NOT NULL,
                    FOREIGN KEY (company_id) REFERENCES companies (id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS unrecognized_shipments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shipment_id INTEGER NOT NULL UNIQUE,
                    tracking_number TEXT NOT NULL,
                    operator_name TEXT NOT NULL DEFAULT '',
                    scanned_at TEXT NOT NULL,
                    resolved_company_name TEXT,
                    resolved_at TEXT,
                    FOREIGN KEY (shipment_id) REFERENCES shipments (id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS duplicate_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracking_number TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    operator_name TEXT NOT NULL DEFAULT '',
                    duplicate_at TEXT NOT NULL,
                    duplicate_day TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    reason_note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS anomaly_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracking_number TEXT NOT NULL,
                    operator_name TEXT NOT NULL DEFAULT '',
                    anomaly_type TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    event_day TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_shipments_tracking_number
                ON shipments (tracking_number)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_shipments_shipping_day
                ON shipments (shipping_day)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_unrecognized_resolved_at
                ON unrecognized_shipments (resolved_at)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_duplicate_events_duplicate_day
                ON duplicate_events (duplicate_day)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_anomaly_events_event_day
                ON anomaly_events (event_day)
                """
            )
        self.conn.commit()
        if self.mode in {"full", "config", "monthly"}:
            self._migrate_companies_table()
            if self.mode in {"full", "config"}:
                self._initialize_settings()
        if self.mode in {"full", "monthly"}:
            self._migrate_shipments_table()
            self._migrate_unrecognized_table()
            self._migrate_duplicate_table()

    def _migrate_companies_table(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(companies)")
        columns = cursor.fetchall()
        column_names = {column["name"] for column in columns}

        if "color" not in column_names:
            cursor.execute(f"ALTER TABLE companies ADD COLUMN color TEXT NOT NULL DEFAULT '{DEFAULT_COMPANY_COLOR}'")
            self.conn.commit()

        name_is_unique = False
        cursor.execute("PRAGMA index_list(companies)")
        for index in cursor.fetchall():
            if not index["unique"]:
                continue
            cursor.execute(f"PRAGMA index_info({index['name']})")
            indexed_columns = cursor.fetchall()
            if len(indexed_columns) == 1 and indexed_columns[0]["name"] == "name":
                name_is_unique = True
                break

        if not name_is_unique:
            return

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS companies_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                prefix TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL DEFAULT '#0B5CAB',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO companies_new (id, name, prefix, color, created_at)
            SELECT id, name, prefix, COALESCE(color, '#0B5CAB'), created_at
            FROM companies
            """
        )
        cursor.execute("DROP TABLE companies")
        cursor.execute("ALTER TABLE companies_new RENAME TO companies")
        self.conn.commit()

    def _migrate_shipments_table(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(shipments)")
        columns = cursor.fetchall()
        column_names = {column["name"] for column in columns}
        if "operator_name" not in column_names:
            cursor.execute("ALTER TABLE shipments ADD COLUMN operator_name TEXT NOT NULL DEFAULT ''")
            self.conn.commit()

    def _migrate_unrecognized_table(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(unrecognized_shipments)")
        columns = cursor.fetchall()
        if not columns:
            return
        column_names = {column["name"] for column in columns}
        if "operator_name" not in column_names:
            cursor.execute("ALTER TABLE unrecognized_shipments ADD COLUMN operator_name TEXT NOT NULL DEFAULT ''")
        if "resolved_company_name" not in column_names:
            cursor.execute("ALTER TABLE unrecognized_shipments ADD COLUMN resolved_company_name TEXT")
        if "resolved_at" not in column_names:
            cursor.execute("ALTER TABLE unrecognized_shipments ADD COLUMN resolved_at TEXT")
        self.conn.commit()

    def _migrate_duplicate_table(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(duplicate_events)")
        columns = cursor.fetchall()
        if not columns:
            return
        column_names = {column["name"] for column in columns}
        if "reason_note" not in column_names:
            cursor.execute("ALTER TABLE duplicate_events ADD COLUMN reason_note TEXT NOT NULL DEFAULT ''")
            self.conn.commit()

    def _initialize_settings(self) -> None:
        self.set_setting_if_missing("duplicate_policy", "all")
        self.set_setting_if_missing("sound_enabled", "1")
        self.set_setting_if_missing("block_unrecognized_enabled", "0")
        self.set_setting_if_missing("operator_shortcuts", "")
        self.set_setting_if_missing("update_manifest_url", DEFAULT_UPDATE_MANIFEST_URL)
        self.set_setting_if_missing("language_code", DEFAULT_LANGUAGE_CODE)

    def set_setting_if_missing(self, key: str, value: str) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO settings (key, value)
            VALUES (?, ?)
            """,
            (key, value),
        )
        self.conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_duplicate_policy(self) -> str:
        value = self.get_setting("duplicate_policy", "all")
        return value if value in {"all", "today"} else "all"

    def set_duplicate_policy(self, value: str) -> None:
        self.set_setting("duplicate_policy", value if value in {"all", "today"} else "all")

    def is_sound_enabled(self) -> bool:
        return self.get_setting("sound_enabled", "1") == "1"

    def set_sound_enabled(self, enabled: bool) -> None:
        self.set_setting("sound_enabled", "1" if enabled else "0")

    def is_block_unrecognized_enabled(self) -> bool:
        return self.get_setting("block_unrecognized_enabled", "0") == "1"

    def set_block_unrecognized_enabled(self, enabled: bool) -> None:
        self.set_setting("block_unrecognized_enabled", "1" if enabled else "0")

    def get_operator_shortcuts(self) -> list[str]:
        raw_value = self.get_setting("operator_shortcuts", "")
        return [item for item in raw_value.split("|") if item]

    def save_operator_shortcut(self, operator_name: str, limit: int = 6) -> list[str]:
        normalized = operator_name.strip()
        shortcuts = [item for item in self.get_operator_shortcuts() if item != normalized]
        if normalized:
            shortcuts.insert(0, normalized)
        shortcuts = shortcuts[:limit]
        self.set_setting("operator_shortcuts", "|".join(shortcuts))
        return shortcuts

    def delete_operator_shortcut(self, operator_name: str) -> list[str]:
        shortcuts = [item for item in self.get_operator_shortcuts() if item != operator_name]
        self.set_setting("operator_shortcuts", "|".join(shortcuts))
        return shortcuts

    def get_blocked_tracking_numbers(self) -> list[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, tracking_number, entry_type, notes, created_at
            FROM blocked_tracking_numbers
            ORDER BY created_at DESC, tracking_number ASC
            """
        )
        return cursor.fetchall()

    def upsert_blocked_tracking_number(self, tracking_number: str, entry_type: str, notes: str = "") -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO blocked_tracking_numbers (tracking_number, entry_type, notes)
            VALUES (?, ?, ?)
            ON CONFLICT(tracking_number) DO UPDATE SET
                entry_type = excluded.entry_type,
                notes = excluded.notes
            """,
            (tracking_number.strip().upper(), entry_type, notes.strip()),
        )
        self.conn.commit()

    def delete_blocked_tracking_number(self, blocked_id: int) -> None:
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM blocked_tracking_numbers WHERE id = ?", (blocked_id,))
        self.conn.commit()

    def get_blocked_tracking_entry(self, tracking_number: str) -> sqlite3.Row | None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT tracking_number, entry_type, notes
            FROM blocked_tracking_numbers
            WHERE tracking_number = ?
            LIMIT 1
            """,
            (tracking_number.strip().upper(),),
        )
        return cursor.fetchone()

    def insert_anomaly_event(self, tracking_number: str, operator_name: str, anomaly_type: str, notes: str = "") -> None:
        cursor = self.conn.cursor()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        event_day = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            """
            INSERT INTO anomaly_events (tracking_number, operator_name, anomaly_type, notes, created_at, event_day)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tracking_number.strip().upper(), operator_name.strip(), anomaly_type, notes.strip(), created_at, event_day),
        )
        self.conn.commit()

    def get_today_anomaly_events(self) -> list[sqlite3.Row]:
        cursor = self.conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            """
            SELECT tracking_number, operator_name, anomaly_type, notes, created_at
            FROM anomaly_events
            WHERE event_day = ?
            ORDER BY created_at DESC
            """,
            (today,),
        )
        return cursor.fetchall()

    def update_duplicate_reason(self, event_id: int, reason_note: str) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE duplicate_events
            SET reason_note = ?
            WHERE id = ?
            """,
            (reason_note.strip(), event_id),
        )
        self.conn.commit()

    def get_companies(self) -> list[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, name, prefix, color
            FROM companies
            ORDER BY LENGTH(prefix) DESC, prefix ASC
            """
        )
        return cursor.fetchall()

    def get_company_rules(self) -> list[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, name, prefix, color
            FROM companies
            ORDER BY name ASC, LENGTH(prefix) DESC, prefix ASC
            """
        )
        return cursor.fetchall()

    def get_company_filter_names(self) -> list[str]:
        cursor = self.conn.cursor()
        if self.mode == "config":
            cursor.execute(
                """
                SELECT DISTINCT name
                FROM companies
                ORDER BY name ASC
                """
            )
        else:
            cursor.execute(
                """
                SELECT DISTINCT company_name AS name
                FROM shipments
                UNION
                SELECT DISTINCT name
                FROM companies
                ORDER BY name ASC
                """
            )
        return [row["name"] for row in cursor.fetchall() if row["name"]]

    def upsert_company(self, company_id: int | None, name: str, prefix: str, color: str) -> None:
        cursor = self.conn.cursor()
        if company_id:
            cursor.execute(
                """
                UPDATE companies
                SET name = ?, prefix = ?, color = ?
                WHERE id = ?
                """,
                (name, prefix, color, company_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO companies (name, prefix, color)
                VALUES (?, ?, ?)
                """,
                (name, prefix, color),
            )
        self.conn.commit()

    def delete_company(self, company_id: int) -> None:
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        self.conn.commit()

    def resolve_company(self, tracking_number: str) -> tuple[int | None, str, str]:
        normalized = tracking_number.strip().upper()
        for row in self.get_companies():
            if normalized.startswith(row["prefix"].upper()):
                return row["id"], row["name"], row["color"]
        if self.translate:
            return None, self.translate("unrecognized"), UNRECOGNIZED_COLOR
        return None, "未识别", UNRECOGNIZED_COLOR

    def get_company_color_by_name(self, company_name: str) -> str:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT color
            FROM companies
            WHERE name = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (company_name,),
        )
        row = cursor.fetchone()
        return row["color"] if row else UNRECOGNIZED_COLOR

    def get_last_shipment(self, tracking_number: str, duplicate_policy: str | None = None) -> sqlite3.Row | None:
        policy = duplicate_policy or self.get_duplicate_policy()
        cursor = self.conn.cursor()
        normalized = tracking_number.strip().upper()
        if policy == "today":
            today = datetime.now().strftime("%Y-%m-%d")
            cursor.execute(
                """
                SELECT tracking_number, company_name, shipped_at
                FROM shipments
                WHERE tracking_number = ? AND shipping_day = ?
                ORDER BY shipped_at DESC
                LIMIT 1
                """,
                (normalized, today),
            )
        else:
            cursor.execute(
                """
                SELECT tracking_number, company_name, shipped_at
                FROM shipments
                WHERE tracking_number = ?
                ORDER BY shipped_at DESC
                LIMIT 1
                """,
                (normalized,),
            )
        return cursor.fetchone()

    def insert_shipment(self, tracking_number: str, operator_name: str) -> tuple[sqlite3.Row | None, dict]:
        normalized = tracking_number.strip().upper()
        last_shipment = self.get_last_shipment(normalized)
        company_id, company_name, company_color = self.resolve_company(normalized)
        shipped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        shipping_day = datetime.now().strftime("%Y-%m-%d")

        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO shipments (tracking_number, company_id, company_name, operator_name, shipped_at, shipping_day)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (normalized, company_id, company_name, operator_name, shipped_at, shipping_day),
        )
        shipment_id = cursor.lastrowid
        self.conn.commit()

        if company_id is None:
            self._insert_unrecognized_shipment(shipment_id, normalized, operator_name, shipped_at)

        return last_shipment, {
            "shipment_id": shipment_id,
            "tracking_number": normalized,
            "company_name": company_name,
            "company_color": company_color,
            "operator_name": operator_name,
            "shipped_at": shipped_at,
            "shipping_day": shipping_day,
        }

    def _insert_unrecognized_shipment(self, shipment_id: int, tracking_number: str, operator_name: str, scanned_at: str) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO unrecognized_shipments (shipment_id, tracking_number, operator_name, scanned_at)
            VALUES (?, ?, ?, ?)
            """,
            (shipment_id, tracking_number, operator_name, scanned_at),
        )
        self.conn.commit()

    def save_shipment_if_new(self, tracking_number: str, operator_name: str) -> tuple[bool, sqlite3.Row | None, dict]:
        normalized = tracking_number.strip().upper()
        blocked_entry = self.get_blocked_tracking_entry(normalized)
        if blocked_entry:
            anomaly_type = "blacklist" if blocked_entry["entry_type"] == "blacklist" else "locked"
            self.insert_anomaly_event(normalized, operator_name, anomaly_type, blocked_entry["notes"])
            return False, None, {
                "tracking_number": normalized,
                "company_name": self.translate("unrecognized") if self.translate else "未识别",
                "company_color": UNRECOGNIZED_COLOR,
                "shipped_at": "",
                "operator_name": operator_name,
                "anomaly_type": anomaly_type,
                "notes": blocked_entry["notes"] or "",
            }

        if len(normalized) < 6:
            self.insert_anomaly_event(normalized, operator_name, "format", "tracking_too_short")
            return False, None, {
                "tracking_number": normalized,
                "company_name": self.translate("unrecognized") if self.translate else "未识别",
                "company_color": UNRECOGNIZED_COLOR,
                "shipped_at": "",
                "operator_name": operator_name,
                "anomaly_type": "format",
                "notes": "tracking_too_short",
            }

        last_shipment = self.get_last_shipment(normalized, self.get_duplicate_policy())
        if last_shipment:
            self.insert_duplicate_event(normalized, last_shipment["company_name"], operator_name, last_shipment["shipped_at"])
            return False, last_shipment, {
                "tracking_number": normalized,
                "company_name": last_shipment["company_name"],
                "company_color": self.get_company_color_by_name(last_shipment["company_name"]),
                "shipped_at": last_shipment["shipped_at"],
                "operator_name": operator_name,
                "anomaly_type": "",
            }

        company_id, company_name, company_color = self.resolve_company(normalized)
        if company_id is None and self.is_block_unrecognized_enabled():
            self.insert_anomaly_event(normalized, operator_name, "unrecognized", "")
            return False, None, {
                "tracking_number": normalized,
                "company_name": company_name,
                "company_color": company_color,
                "shipped_at": "",
                "operator_name": operator_name,
                "anomaly_type": "unrecognized",
                "notes": "",
            }

        _, new_shipment = self.insert_shipment(normalized, operator_name)
        if new_shipment["company_name"] == (self.translate("unrecognized") if self.translate else "未识别"):
            self.insert_anomaly_event(normalized, operator_name, "unrecognized", "")
        return True, None, new_shipment

    def insert_duplicate_event(self, tracking_number: str, company_name: str, operator_name: str, last_seen_at: str) -> None:
        cursor = self.conn.cursor()
        duplicate_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        duplicate_day = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            """
            INSERT INTO duplicate_events (tracking_number, company_name, operator_name, duplicate_at, duplicate_day, last_seen_at, reason_note)
            VALUES (?, ?, ?, ?, ?, ?, '')
            """,
            (tracking_number, company_name, operator_name, duplicate_at, duplicate_day, last_seen_at),
        )
        self.conn.commit()

    def reprocess_unrecognized_shipments(self) -> int:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, shipment_id, tracking_number
            FROM unrecognized_shipments
            WHERE resolved_at IS NULL
            ORDER BY scanned_at ASC
            """
        )
        rows = cursor.fetchall()
        resolved_count = 0
        resolved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for row in rows:
            company_id, company_name, _company_color = self.resolve_company(row["tracking_number"])
            if company_id is None:
                continue
            cursor.execute(
                """
                UPDATE shipments
                SET company_id = ?, company_name = ?
                WHERE id = ?
                """,
                (company_id, company_name, row["shipment_id"]),
            )
            cursor.execute(
                """
                UPDATE unrecognized_shipments
                SET resolved_company_name = ?, resolved_at = ?
                WHERE id = ?
                """,
                (company_name, resolved_at, row["id"]),
            )
            resolved_count += 1

        self.conn.commit()
        return resolved_count

    def get_today_company_counts(self) -> list[sqlite3.Row]:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.get_company_stats(today, today)

    def get_today_total(self) -> int:
        cursor = self.conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute("SELECT COUNT(*) AS total FROM shipments WHERE shipping_day = ?", (today,))
        return int(cursor.fetchone()["total"])

    def get_current_hour_total(self) -> int:
        cursor = self.conn.cursor()
        current_hour = datetime.now().strftime("%Y-%m-%d %H")
        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM shipments
            WHERE strftime('%Y-%m-%d %H', shipped_at) = ?
            """,
            (current_hour,),
        )
        return int(cursor.fetchone()["total"])

    def get_today_duplicate_total(self) -> int:
        cursor = self.conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM duplicate_events
            WHERE duplicate_day = ?
            """,
            (today,),
        )
        return int(cursor.fetchone()["total"])

    def get_today_duplicate_events(self) -> list[sqlite3.Row]:
        cursor = self.conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            """
            SELECT id, tracking_number, company_name, operator_name, duplicate_at, last_seen_at, reason_note
            FROM duplicate_events
            WHERE duplicate_day = ?
            ORDER BY duplicate_at DESC
            """,
            (today,),
        )
        return cursor.fetchall()

    def get_throughput_metrics(self) -> dict[str, float]:
        cursor = self.conn.cursor()
        now = datetime.now()
        last_10_minutes = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        last_1_hour = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("SELECT COUNT(*) AS total FROM shipments WHERE shipped_at >= ?", (last_10_minutes,))
        count_10_min = int(cursor.fetchone()["total"])
        cursor.execute("SELECT COUNT(*) AS total FROM shipments WHERE shipped_at >= ?", (last_1_hour,))
        count_1_hour = int(cursor.fetchone()["total"])
        return {
            "last_10_min": count_10_min,
            "last_1_hour": count_1_hour,
            "avg_per_min": round(count_1_hour / 60, 2),
        }

    def archive_old_data(self, cutoff_day: str, archive_dir: Path) -> Path | None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) AS total FROM shipments WHERE shipping_day < ?", (cutoff_day,))
        shipment_total = int(cursor.fetchone()["total"])
        cursor.execute("SELECT COUNT(*) AS total FROM duplicate_events WHERE duplicate_day < ?", (cutoff_day,))
        duplicate_total = int(cursor.fetchone()["total"])
        cursor.execute("SELECT COUNT(*) AS total FROM anomaly_events WHERE event_day < ?", (cutoff_day,))
        anomaly_total = int(cursor.fetchone()["total"])
        if shipment_total == 0 and duplicate_total == 0 and anomaly_total == 0:
            return None

        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"courier_archive_before_{cutoff_day.replace('-', '')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        archive_conn = sqlite3.connect(archive_path)
        try:
            self.conn.backup(archive_conn)
            archive_cursor = archive_conn.cursor()
            archive_cursor.execute("DELETE FROM shipments WHERE shipping_day >= ?", (cutoff_day,))
            archive_cursor.execute("DELETE FROM duplicate_events WHERE duplicate_day >= ?", (cutoff_day,))
            archive_cursor.execute("DELETE FROM anomaly_events WHERE event_day >= ?", (cutoff_day,))
            archive_cursor.execute("DELETE FROM unrecognized_shipments WHERE scanned_at >= ?", (f"{cutoff_day} 00:00:00",))
            archive_conn.commit()
        finally:
            archive_conn.close()

        cursor.execute("DELETE FROM shipments WHERE shipping_day < ?", (cutoff_day,))
        cursor.execute("DELETE FROM duplicate_events WHERE duplicate_day < ?", (cutoff_day,))
        cursor.execute("DELETE FROM anomaly_events WHERE event_day < ?", (cutoff_day,))
        cursor.execute("DELETE FROM unrecognized_shipments WHERE scanned_at < ?", (f"{cutoff_day} 00:00:00",))
        self.conn.commit()
        return archive_path

    def _date_filters(
        self,
        start_day: str | None = None,
        end_day: str | None = None,
        company_name: str | None = None,
        default_days: int | None = None,
    ) -> tuple[list[str], list[str]]:
        clauses: list[str] = []
        params: list[str] = []

        if not start_day and not end_day and default_days:
            start_day = (datetime.now() - timedelta(days=default_days - 1)).strftime("%Y-%m-%d")

        if start_day:
            clauses.append("shipping_day >= ?")
            params.append(start_day)
        if end_day:
            clauses.append("shipping_day <= ?")
            params.append(end_day)
        if company_name:
            clauses.append("company_name = ?")
            params.append(company_name)
        return clauses, params

    def get_daily_stats(
        self,
        start_day: str | None = None,
        end_day: str | None = None,
        company_name: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses, params = self._date_filters(start_day, end_day, company_name, default_days=30)
        cursor = self.conn.cursor()
        query = """
            SELECT shipping_day, COUNT(*) AS total
            FROM shipments
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " GROUP BY shipping_day ORDER BY shipping_day DESC"
        cursor.execute(query, params)
        return cursor.fetchall()

    def get_company_stats(
        self,
        start_day: str | None = None,
        end_day: str | None = None,
        company_name: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses, params = self._date_filters(start_day, end_day, company_name, default_days=None)
        cursor = self.conn.cursor()
        query = """
            SELECT s.company_name,
                   COUNT(*) AS total,
                   COALESCE(
                       (
                           SELECT c.color
                           FROM companies c
                           WHERE c.name = s.company_name
                           ORDER BY c.id DESC
                           LIMIT 1
                       ),
                       ?
                   ) AS color
            FROM shipments s
        """
        params = [UNRECOGNIZED_COLOR] + params
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " GROUP BY company_name ORDER BY total DESC, company_name ASC"
        cursor.execute(query, params)
        return cursor.fetchall()

    def get_operator_stats(
        self,
        start_day: str | None = None,
        end_day: str | None = None,
        company_name: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses, params = self._date_filters(start_day, end_day, company_name, default_days=None)
        cursor = self.conn.cursor()
        query = """
            SELECT CASE WHEN TRIM(operator_name) = '' THEN '-' ELSE operator_name END AS operator_name,
                   COUNT(*) AS total
            FROM shipments
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " GROUP BY operator_name ORDER BY total DESC, operator_name ASC"
        cursor.execute(query, params)
        return cursor.fetchall()

    def query_tracking_number(self, tracking_number: str) -> list[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT tracking_number, company_name, operator_name, shipped_at, shipping_day
            FROM shipments
            WHERE tracking_number = ?
            ORDER BY shipped_at DESC
            """,
            (tracking_number.strip().upper(),),
        )
        return cursor.fetchall()

    def get_recent_shipments(self, limit: int = 100) -> list[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, tracking_number, company_name, operator_name, shipped_at
            FROM shipments
            ORDER BY shipped_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cursor.fetchall()

    def delete_shipment(self, shipment_id: int) -> None:
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM unrecognized_shipments WHERE shipment_id = ?", (shipment_id,))
        cursor.execute("DELETE FROM shipments WHERE id = ?", (shipment_id,))
        self.conn.commit()

    def get_all_shipments(
        self,
        start_day: str | None = None,
        end_day: str | None = None,
        company_name: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses, params = self._date_filters(start_day, end_day, company_name, default_days=None)
        cursor = self.conn.cursor()
        query = """
            SELECT s.tracking_number,
                   s.company_name,
                   s.operator_name,
                   s.shipped_at,
                   s.shipping_day,
                   COALESCE(
                       (
                           SELECT c.color
                           FROM companies c
                           WHERE c.name = s.company_name
                           ORDER BY c.id DESC
                           LIMIT 1
                       ),
                       ?
                   ) AS color
            FROM shipments s
        """
        params = [UNRECOGNIZED_COLOR] + params
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY shipped_at DESC"
        cursor.execute(query, params)
        return cursor.fetchall()

    def get_unrecognized_shipments(self) -> list[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, tracking_number, operator_name, scanned_at
            FROM unrecognized_shipments
            WHERE resolved_at IS NULL
            ORDER BY scanned_at DESC
            """
        )
        return cursor.fetchall()


class MonthlyDatabaseManager:
    def __init__(self, config_db_path: Path, translate: Any | None = None) -> None:
        self.translate = translate
        self.config_db = Database(config_db_path, translate, mode="config")
        self.month_dbs: dict[str, Database] = {}
        self._ensure_month_db(month_key_from_date())

    def close(self) -> None:
        self.config_db.close()
        for db in self.month_dbs.values():
            db.close()
        self.month_dbs.clear()

    def _ensure_month_db(self, month_key: str) -> Database:
        if month_key not in self.month_dbs:
            month_db = Database(db_path_for_month(month_key), self.translate, mode="monthly")
            self._sync_reference_tables(month_db)
            self.month_dbs[month_key] = month_db
        return self.month_dbs[month_key]

    def _sync_reference_tables(self, month_db: Database) -> None:
        config_cursor = self.config_db.conn.cursor()
        month_cursor = month_db.conn.cursor()

        month_cursor.execute("DELETE FROM companies")
        company_rows = config_cursor.execute("SELECT id, name, prefix, color, created_at FROM companies ORDER BY id ASC").fetchall()
        if company_rows:
            month_cursor.executemany(
                """
                INSERT INTO companies (id, name, prefix, color, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(row["id"], row["name"], row["prefix"], row["color"], row["created_at"]) for row in company_rows],
            )

        month_cursor.execute("DELETE FROM settings")
        setting_rows = config_cursor.execute("SELECT key, value FROM settings").fetchall()
        if setting_rows:
            month_cursor.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                [(row["key"], row["value"]) for row in setting_rows],
            )

        month_cursor.execute("DELETE FROM blocked_tracking_numbers")
        blocked_rows = config_cursor.execute(
            "SELECT id, tracking_number, entry_type, notes, created_at FROM blocked_tracking_numbers ORDER BY id ASC"
        ).fetchall()
        if blocked_rows:
            month_cursor.executemany(
                """
                INSERT INTO blocked_tracking_numbers (id, tracking_number, entry_type, notes, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(row["id"], row["tracking_number"], row["entry_type"], row["notes"], row["created_at"]) for row in blocked_rows],
            )

        month_db.conn.commit()

    def _sync_open_month_dbs(self) -> None:
        for month_db in self.month_dbs.values():
            self._sync_reference_tables(month_db)

    def _current_month_key(self) -> str:
        return month_key_from_date()

    def _current_db(self) -> Database:
        return self._ensure_month_db(self._current_month_key())

    def list_month_keys(self) -> list[str]:
        month_keys = {self._current_month_key()}
        month_keys.update(self.month_dbs.keys())
        for path in list_month_db_paths():
            month_key = month_key_from_db_path(path)
            if month_key:
                month_keys.add(month_key)
        return sorted(month_keys, reverse=True)

    def get_month_db_paths(self) -> list[Path]:
        return [CONFIG_DB_PATH] + [db_path_for_month(month_key) for month_key in self.list_month_keys()]

    def replace_databases(self, replacement_paths: dict[str, Path]) -> None:
        self.close()
        for existing_path in self.get_month_db_paths():
            if existing_path.name == CONFIG_DB_NAME and CONFIG_DB_NAME not in replacement_paths:
                continue
            if existing_path.exists():
                existing_path.unlink()
        for target_name, source_path in replacement_paths.items():
            shutil.copy2(source_path, APP_DIR / target_name)

    def _iter_month_dbs(
        self,
        selected_month: str | None = None,
        start_day: str | None = None,
        end_day: str | None = None,
    ) -> list[tuple[str, Database]]:
        if selected_month and selected_month != ALL_MONTHS_VALUE:
            return [(selected_month, self._ensure_month_db(selected_month))]

        start_month = start_day[:7] if start_day else None
        end_month = end_day[:7] if end_day else None
        result: list[tuple[str, Database]] = []
        for month_key in self.list_month_keys():
            if start_month and month_key < start_month:
                continue
            if end_month and month_key > end_month:
                continue
            result.append((month_key, self._ensure_month_db(month_key)))
        return result

    def _aggregate_counts(
        self,
        rows_by_month: list[list[sqlite3.Row]],
        key_name: str,
        value_name: str = "total",
        extra_fields: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        totals: dict[str, dict[str, Any]] = {}
        for rows in rows_by_month:
            for row in rows:
                key = row[key_name]
                if key not in totals:
                    totals[key] = {key_name: key, value_name: 0}
                    for field in extra_fields:
                        totals[key][field] = row[field]
                totals[key][value_name] += int(row[value_name])
        return list(totals.values())

    def get_setting(self, key: str, default: str = "") -> str:
        return self.config_db.get_setting(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.config_db.set_setting(key, value)
        self._sync_open_month_dbs()

    def get_duplicate_policy(self) -> str:
        return self.config_db.get_duplicate_policy()

    def set_duplicate_policy(self, value: str) -> None:
        self.config_db.set_duplicate_policy(value)
        self._sync_open_month_dbs()

    def is_sound_enabled(self) -> bool:
        return self.config_db.is_sound_enabled()

    def set_sound_enabled(self, enabled: bool) -> None:
        self.config_db.set_sound_enabled(enabled)
        self._sync_open_month_dbs()

    def is_block_unrecognized_enabled(self) -> bool:
        return self.config_db.is_block_unrecognized_enabled()

    def set_block_unrecognized_enabled(self, enabled: bool) -> None:
        self.config_db.set_block_unrecognized_enabled(enabled)
        self._sync_open_month_dbs()

    def get_operator_shortcuts(self) -> list[str]:
        return self.config_db.get_operator_shortcuts()

    def save_operator_shortcut(self, operator_name: str, limit: int = 6) -> list[str]:
        shortcuts = self.config_db.save_operator_shortcut(operator_name, limit)
        self._sync_open_month_dbs()
        return shortcuts

    def delete_operator_shortcut(self, operator_name: str) -> list[str]:
        shortcuts = self.config_db.delete_operator_shortcut(operator_name)
        self._sync_open_month_dbs()
        return shortcuts

    def get_blocked_tracking_numbers(self) -> list[sqlite3.Row]:
        return self.config_db.get_blocked_tracking_numbers()

    def upsert_blocked_tracking_number(self, tracking_number: str, entry_type: str, notes: str = "") -> None:
        self.config_db.upsert_blocked_tracking_number(tracking_number, entry_type, notes)
        self._sync_open_month_dbs()

    def delete_blocked_tracking_number(self, blocked_id: int) -> None:
        self.config_db.delete_blocked_tracking_number(blocked_id)
        self._sync_open_month_dbs()

    def get_blocked_tracking_entry(self, tracking_number: str) -> sqlite3.Row | None:
        return self.config_db.get_blocked_tracking_entry(tracking_number)

    def update_duplicate_reason(self, event_id: int, reason_note: str) -> None:
        self._current_db().update_duplicate_reason(event_id, reason_note)

    def get_companies(self) -> list[sqlite3.Row]:
        return self.config_db.get_companies()

    def get_company_rules(self) -> list[sqlite3.Row]:
        return self.config_db.get_company_rules()

    def get_company_filter_names(self) -> list[str]:
        names = set(self.config_db.get_company_filter_names())
        for _month_key, db in self._iter_month_dbs():
            for row in db.get_company_stats():
                if row["company_name"]:
                    names.add(row["company_name"])
        return sorted(names)

    def upsert_company(self, company_id: int | None, name: str, prefix: str, color: str) -> None:
        self.config_db.upsert_company(company_id, name, prefix, color)
        self._sync_open_month_dbs()

    def delete_company(self, company_id: int) -> None:
        self.config_db.delete_company(company_id)
        self._sync_open_month_dbs()

    def resolve_company(self, tracking_number: str) -> tuple[int | None, str, str]:
        return self.config_db.resolve_company(tracking_number)

    def get_company_color_by_name(self, company_name: str) -> str:
        return self.config_db.get_company_color_by_name(company_name)

    def get_last_shipment(self, tracking_number: str, duplicate_policy: str | None = None) -> sqlite3.Row | None:
        normalized = tracking_number.strip().upper()
        policy = duplicate_policy or self.get_duplicate_policy()
        if policy == "today":
            return self._current_db().get_last_shipment(normalized, "today")

        last_row: sqlite3.Row | None = None
        for _month_key, db in self._iter_month_dbs():
            row = db.get_last_shipment(normalized, "all")
            if row and (last_row is None or row["shipped_at"] > last_row["shipped_at"]):
                last_row = row
        return last_row

    def save_shipment_if_new(self, tracking_number: str, operator_name: str) -> tuple[bool, sqlite3.Row | None, dict]:
        normalized = tracking_number.strip().upper()
        blocked_entry = self.get_blocked_tracking_entry(normalized)
        if blocked_entry:
            anomaly_type = "blacklist" if blocked_entry["entry_type"] == "blacklist" else "locked"
            self._current_db().insert_anomaly_event(normalized, operator_name, anomaly_type, blocked_entry["notes"])
            return False, None, {
                "tracking_number": normalized,
                "company_name": self.translate("unrecognized") if self.translate else "未识别",
                "company_color": UNRECOGNIZED_COLOR,
                "shipped_at": "",
                "operator_name": operator_name,
                "anomaly_type": anomaly_type,
                "notes": blocked_entry["notes"] or "",
            }

        if len(normalized) < 6:
            self._current_db().insert_anomaly_event(normalized, operator_name, "format", "tracking_too_short")
            return False, None, {
                "tracking_number": normalized,
                "company_name": self.translate("unrecognized") if self.translate else "未识别",
                "company_color": UNRECOGNIZED_COLOR,
                "shipped_at": "",
                "operator_name": operator_name,
                "anomaly_type": "format",
                "notes": "tracking_too_short",
            }

        last_shipment = self.get_last_shipment(normalized, self.get_duplicate_policy())
        if last_shipment:
            self._current_db().insert_duplicate_event(normalized, last_shipment["company_name"], operator_name, last_shipment["shipped_at"])
            return False, last_shipment, {
                "tracking_number": normalized,
                "company_name": last_shipment["company_name"],
                "company_color": self.get_company_color_by_name(last_shipment["company_name"]),
                "shipped_at": last_shipment["shipped_at"],
                "operator_name": operator_name,
                "anomaly_type": "",
            }

        company_id, company_name, company_color = self.resolve_company(normalized)
        if company_id is None and self.is_block_unrecognized_enabled():
            self._current_db().insert_anomaly_event(normalized, operator_name, "unrecognized", "")
            return False, None, {
                "tracking_number": normalized,
                "company_name": company_name,
                "company_color": company_color,
                "shipped_at": "",
                "operator_name": operator_name,
                "anomaly_type": "unrecognized",
                "notes": "",
            }

        shipped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        shipping_day = datetime.now().strftime("%Y-%m-%d")
        current_db = self._current_db()
        cursor = current_db.conn.cursor()
        cursor.execute(
            """
            INSERT INTO shipments (tracking_number, company_id, company_name, operator_name, shipped_at, shipping_day)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (normalized, company_id, company_name, operator_name, shipped_at, shipping_day),
        )
        shipment_id = cursor.lastrowid
        current_db.conn.commit()

        if company_id is None:
            current_db._insert_unrecognized_shipment(shipment_id, normalized, operator_name, shipped_at)
            current_db.insert_anomaly_event(normalized, operator_name, "unrecognized", "")

        return True, None, {
            "shipment_id": shipment_id,
            "tracking_number": normalized,
            "company_name": company_name,
            "company_color": company_color,
            "operator_name": operator_name,
            "shipped_at": shipped_at,
            "shipping_day": shipping_day,
        }

    def reprocess_unrecognized_shipments(self) -> int:
        resolved_total = 0
        resolved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for _month_key, db in self._iter_month_dbs():
            cursor = db.conn.cursor()
            cursor.execute(
                """
                SELECT id, shipment_id, tracking_number
                FROM unrecognized_shipments
                WHERE resolved_at IS NULL
                ORDER BY scanned_at ASC
                """
            )
            rows = cursor.fetchall()
            for row in rows:
                company_id, company_name, _company_color = self.resolve_company(row["tracking_number"])
                if company_id is None:
                    continue
                cursor.execute(
                    """
                    UPDATE shipments
                    SET company_id = ?, company_name = ?
                    WHERE id = ?
                    """,
                    (company_id, company_name, row["shipment_id"]),
                )
                cursor.execute(
                    """
                    UPDATE unrecognized_shipments
                    SET resolved_company_name = ?, resolved_at = ?
                    WHERE id = ?
                    """,
                    (company_name, resolved_at, row["id"]),
                )
                resolved_total += 1
            db.conn.commit()
        return resolved_total

    def get_today_company_counts(self) -> list[sqlite3.Row]:
        return self._current_db().get_today_company_counts()

    def get_today_total(self) -> int:
        return self._current_db().get_today_total()

    def get_current_hour_total(self) -> int:
        return self._current_db().get_current_hour_total()

    def get_today_duplicate_total(self) -> int:
        return self._current_db().get_today_duplicate_total()

    def get_today_duplicate_events(self) -> list[sqlite3.Row]:
        return self._current_db().get_today_duplicate_events()

    def get_today_anomaly_events(self) -> list[sqlite3.Row]:
        return self._current_db().get_today_anomaly_events()

    def get_throughput_metrics(self) -> dict[str, float]:
        return self._current_db().get_throughput_metrics()

    def get_daily_stats(
        self,
        start_day: str | None = None,
        end_day: str | None = None,
        company_name: str | None = None,
        selected_month: str | None = None,
    ) -> list[dict[str, Any]]:
        if not start_day and not end_day and not selected_month:
            start_day = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
        aggregated = self._aggregate_counts(
            [db.get_daily_stats(start_day, end_day, company_name) for _month_key, db in self._iter_month_dbs(selected_month, start_day, end_day)],
            "shipping_day",
        )
        return sorted(aggregated, key=lambda row: row["shipping_day"], reverse=True)

    def get_company_stats(
        self,
        start_day: str | None = None,
        end_day: str | None = None,
        company_name: str | None = None,
        selected_month: str | None = None,
    ) -> list[dict[str, Any]]:
        aggregated = self._aggregate_counts(
            [db.get_company_stats(start_day, end_day, company_name) for _month_key, db in self._iter_month_dbs(selected_month, start_day, end_day)],
            "company_name",
            extra_fields=("color",),
        )
        for row in aggregated:
            row["color"] = self.get_company_color_by_name(row["company_name"])
        return sorted(aggregated, key=lambda row: (-row["total"], row["company_name"]))

    def get_operator_stats(
        self,
        start_day: str | None = None,
        end_day: str | None = None,
        company_name: str | None = None,
        selected_month: str | None = None,
    ) -> list[dict[str, Any]]:
        aggregated = self._aggregate_counts(
            [db.get_operator_stats(start_day, end_day, company_name) for _month_key, db in self._iter_month_dbs(selected_month, start_day, end_day)],
            "operator_name",
        )
        return sorted(aggregated, key=lambda row: (-row["total"], row["operator_name"]))

    def query_tracking_number(self, tracking_number: str, selected_month: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for month_key, db in self._iter_month_dbs(selected_month):
            for row in db.query_tracking_number(tracking_number):
                row_dict = dict(row)
                row_dict["month_key"] = month_key
                rows.append(row_dict)
        return sorted(rows, key=lambda row: row["shipped_at"], reverse=True)

    def get_recent_shipments(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._current_db().get_recent_shipments(limit)

    def delete_shipment(self, shipment_id: int) -> None:
        self._current_db().delete_shipment(shipment_id)

    def get_all_shipments(
        self,
        start_day: str | None = None,
        end_day: str | None = None,
        company_name: str | None = None,
        selected_month: str | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for month_key, db in self._iter_month_dbs(selected_month, start_day, end_day):
            for row in db.get_all_shipments(start_day, end_day, company_name):
                row_dict = dict(row)
                row_dict["month_key"] = month_key
                rows.append(row_dict)
        return sorted(rows, key=lambda row: row["shipped_at"], reverse=True)

    def get_unrecognized_shipments(self, selected_month: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for month_key, db in self._iter_month_dbs(selected_month):
            for row in db.get_unrecognized_shipments():
                row_dict = dict(row)
                row_dict["month_key"] = month_key
                rows.append(row_dict)
        return sorted(rows, key=lambda row: row["scanned_at"], reverse=True)

    def archive_old_data(self, cutoff_day: str, archive_dir: Path) -> Path | None:
        cutoff_month = cutoff_day[:7]
        month_paths = [db_path_for_month(month_key) for month_key in self.list_month_keys() if month_key < cutoff_month and db_path_for_month(month_key).exists()]
        if not month_paths:
            return None
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"courier_month_archive_before_{cutoff_day.replace('-', '')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
            for month_path in month_paths:
                archive_file.write(month_path, arcname=month_path.name)
        return archive_path

class ReportExporter:
    def __init__(self, db: Any, translate: Any | None = None) -> None:
        self.db = db
        self.translate = translate

    def _label(self, key: str) -> str:
        if self.translate is None:
            return key
        return str(self.translate(key)).rstrip(":")

    def _sheet_xml(self, name: str, headers: list[str], rows: list[list[Any]]) -> str:
        header_cells = "".join(
            f'<Cell ss:StyleID="header"><Data ss:Type="String">{escape(str(value))}</Data></Cell>'
            for value in headers
        )
        xml_rows = [f"<Row>{header_cells}</Row>"]
        for row in rows:
            cells: list[str] = []
            for value in row:
                cell_type = "Number" if isinstance(value, int) else "String"
                cells.append(f'<Cell><Data ss:Type="{cell_type}">{escape(str(value))}</Data></Cell>')
            xml_rows.append("<Row>" + "".join(cells) + "</Row>")
        return f'<Worksheet ss:Name="{escape(name)}"><Table>{"".join(xml_rows)}</Table></Worksheet>'

    def export_report(
        self,
        output_dir: Path,
        start_day: str | None = None,
        end_day: str | None = None,
        company_name: str | None = None,
        selected_month: str | None = None,
        report_tag: str | None = None,
    ) -> tuple[Path, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_suffix = f"{report_tag}_{timestamp}" if report_tag else timestamp
        summary_path = output_dir / f"courier_report_summary_{file_suffix}.xls"
        detail_path = output_dir / f"courier_report_detail_{file_suffix}.xls"

        filtered_counts = [[row["company_name"], row["total"]] for row in self.db.get_company_stats(start_day, end_day, company_name, selected_month)]
        daily_stats = [[row["shipping_day"], row["total"]] for row in self.db.get_daily_stats(start_day, end_day, company_name, selected_month)]
        company_stats = [[row["company_name"], row["color"], row["total"]] for row in self.db.get_company_stats(start_day, end_day, company_name, selected_month)]
        operator_stats = [[row["operator_name"], row["total"]] for row in self.db.get_operator_stats(start_day, end_day, company_name, selected_month)]
        shipment_rows = [
            [row["tracking_number"], row["company_name"], row["color"], row["operator_name"], row["shipped_at"], row["shipping_day"]]
            for row in self.db.get_all_shipments(start_day, end_day, company_name, selected_month)
        ]
        unrecognized_rows = [
            [row["tracking_number"], row["operator_name"], row["scanned_at"]]
            for row in self.db.get_unrecognized_shipments(selected_month)
        ]

        summary_sheets = [
            self._sheet_xml(
                self._label("export_sheet_filtered_companies"),
                [self._label("company_name"), self._label("quantity")],
                filtered_counts,
            ),
            self._sheet_xml(
                self._label("export_sheet_daily_stats"),
                [self._label("date"), self._label("package_total")],
                daily_stats,
            ),
            self._sheet_xml(
                self._label("export_sheet_company_stats"),
                [self._label("company_name"), self._label("export_color"), self._label("cumulative_total")],
                company_stats,
            ),
            self._sheet_xml(
                self._label("export_sheet_operator_stats"),
                [self._label("operator_label"), self._label("quantity")],
                operator_stats,
            ),
        ]
        detail_sheets = [
            self._sheet_xml(
                self._label("export_sheet_shipments"),
                [
                    self._label("tracking_number"),
                    self._label("company_name"),
                    self._label("export_color"),
                    self._label("operator_label"),
                    self._label("shipped_at"),
                    self._label("shipping_day"),
                ],
                shipment_rows,
            ),
            self._sheet_xml(
                self._label("export_sheet_unrecognized"),
                [self._label("tracking_number"), self._label("operator_label"), self._label("export_scanned_at")],
                unrecognized_rows,
            ),
        ]

        summary_workbook = f"""<?xml version="1.0" encoding="UTF-8"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:x="urn:schemas-microsoft-com:office:excel"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Styles>
  <Style ss:ID="header">
   <Font ss:Bold="1"/>
   <Interior ss:Color="#D9EAF7" ss:Pattern="Solid"/>
  </Style>
 </Styles>
 {" ".join(summary_sheets)}
</Workbook>
"""
        detail_workbook = f"""<?xml version="1.0" encoding="UTF-8"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:x="urn:schemas-microsoft-com:office:excel"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Styles>
  <Style ss:ID="header">
   <Font ss:Bold="1"/>
   <Interior ss:Color="#D9EAF7" ss:Pattern="Solid"/>
  </Style>
 </Styles>
 {" ".join(detail_sheets)}
</Workbook>
"""
        summary_path.write_text(summary_workbook, encoding="utf-8")
        detail_path.write_text(detail_workbook, encoding="utf-8")
        return summary_path, detail_path


class BackupManager:
    def __init__(self, db: MonthlyDatabaseManager, backup_dir: Path) -> None:
        self.db = db
        self.backup_dir = backup_dir

    def _timestamped_backup_path(self, prefix: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.backup_dir / f"{prefix}{timestamp}.zip"

    def create_backup(self, prefix: str = MANUAL_BACKUP_PREFIX) -> Path:
        backup_path = self._timestamped_backup_path(prefix)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
            for db_path in self.db.get_month_db_paths():
                if not db_path.exists():
                    continue
                archive_file.write(db_path, arcname=db_path.name)
        return backup_path

    def create_daily_auto_backup(self) -> Path | None:
        backup_path = self.backup_dir / f"{AUTO_BACKUP_PREFIX}{datetime.now().strftime('%Y%m%d')}.zip"
        if backup_path.exists():
            return None
        created_path = self.create_backup(AUTO_BACKUP_PREFIX)
        if created_path != backup_path:
            created_path.replace(backup_path)
            created_path = backup_path
        prune_auto_backups()
        return created_path

    def list_backup_files(self) -> list[Path]:
        return sorted(self.backup_dir.glob("*.zip"), reverse=True)

    def delete_backup(self, backup_path: Path) -> None:
        backup_path.unlink()


class UpdateManager:
    def __init__(self, app_dir: Path, update_dir: Path) -> None:
        self.app_dir = app_dir
        self.update_dir = update_dir

    def is_supported_environment(self) -> bool:
        return sys.platform.startswith("win") and getattr(sys, "frozen", False)

    def should_use_windows_downloader(self) -> bool:
        return sys.platform.startswith("win") and getattr(sys, "frozen", False)

    def run_powershell(self, script: str, *args: str) -> bytes:
        self.update_dir.mkdir(parents=True, exist_ok=True)
        script_path = self.update_dir / f"powershell_task_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.ps1"
        script_path.write_text(script, encoding="utf-8")
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), *args],
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace").strip()
            raise OSError(stderr or str(exc)) from exc
        finally:
            script_path.unlink(missing_ok=True)
        return completed.stdout

    def fetch_text_with_powershell(self, url: str) -> str:
        script = """
param([string]$Url)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$response = Invoke-WebRequest -UseBasicParsing -Uri $Url
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::Write($response.Content)
"""
        return self.run_powershell(script, url).decode("utf-8-sig", errors="replace")

    def download_file_with_powershell(self, url: str, target_path: Path) -> None:
        script = """
param([string]$Url, [string]$OutputPath)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutputPath
"""
        self.run_powershell(script, url, str(target_path))

    def fetch_manifest(self, manifest_url: str) -> dict[str, Any]:
        if self.should_use_windows_downloader():
            payload = self.fetch_text_with_powershell(manifest_url)
        else:
            with urlopen(manifest_url, timeout=20) as response:
                payload = response.read().decode("utf-8")
        manifest = json.loads(payload)
        if not isinstance(manifest, dict):
            raise ValueError("manifest_not_dict")
        required_keys = {"version", "download_url"}
        if not required_keys.issubset(manifest):
            raise ValueError("manifest_missing_keys")
        return manifest

    def version_tuple(self, value: str) -> tuple[int, ...]:
        cleaned = value.strip()
        if not cleaned:
            return (0,)
        parts: list[int] = []
        for item in cleaned.split("."):
            match = re.match(r"(\d+)", item.strip())
            parts.append(int(match.group(1)) if match else 0)
        return tuple(parts or [0])

    def is_newer_version(self, latest_version: str, current_version: str) -> bool:
        latest_parts = self.version_tuple(latest_version)
        current_parts = self.version_tuple(current_version)
        max_len = max(len(latest_parts), len(current_parts))
        latest_parts += (0,) * (max_len - len(latest_parts))
        current_parts += (0,) * (max_len - len(current_parts))
        return latest_parts > current_parts

    def download_update_package(self, download_url: str, expected_sha256: str | None = None) -> Path:
        parsed = urlparse(download_url)
        file_name = Path(parsed.path).name or "courier_update.exe"
        target_path = self.update_dir / file_name
        self.update_dir.mkdir(parents=True, exist_ok=True)
        target_path.unlink(missing_ok=True)
        if self.should_use_windows_downloader():
            self.download_file_with_powershell(download_url, target_path)
        else:
            with urlopen(download_url, timeout=60) as response, target_path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 128)
                    if not chunk:
                        break
                    output.write(chunk)
        if expected_sha256:
            digest = hashlib.sha256(target_path.read_bytes()).hexdigest().lower()
            if digest != expected_sha256.strip().lower():
                target_path.unlink(missing_ok=True)
                raise ValueError("sha256_mismatch")
        return target_path

    def create_windows_upgrade_script(self, current_exe: Path, new_exe: Path, app_pid: int) -> Path:
        self.update_dir.mkdir(parents=True, exist_ok=True)
        script_path = self.update_dir / "apply_update.bat"
        script_content = f"""@echo off
setlocal
set "APP_PID={app_pid}"
set "NEW_EXE={new_exe}"
set "CURRENT_EXE={current_exe}"
for %%I in ("%CURRENT_EXE%") do set "APP_DIR=%%~dpI"
for %%I in ("%CURRENT_EXE%") do set "APP_NAME=%%~nxI"
set "WAIT_COUNT=0"
set "IMAGE_WAIT_COUNT=0"
set "COPY_COUNT=0"

timeout /t 1 /nobreak >nul

:wait_for_app_exit
tasklist /FI "PID eq %APP_PID%" /NH 2>nul | findstr /I "%APP_PID%" >nul
if errorlevel 1 goto wait_for_app_image_exit
set /a WAIT_COUNT+=1
if %WAIT_COUNT% GEQ 8 goto force_close_app
timeout /t 1 /nobreak >nul
goto wait_for_app_exit

:force_close_app
taskkill /PID %APP_PID% /F >nul 2>nul
timeout /t 1 /nobreak >nul

:wait_for_app_image_exit
tasklist /FI "IMAGENAME eq %APP_NAME%" /NH 2>nul | findstr /I "%APP_NAME%" >nul
if errorlevel 1 goto copy_update
set /a IMAGE_WAIT_COUNT+=1
if %IMAGE_WAIT_COUNT% GEQ 6 goto force_close_app_image
timeout /t 1 /nobreak >nul
goto wait_for_app_image_exit

:force_close_app_image
taskkill /IM "%APP_NAME%" /F >nul 2>nul
timeout /t 2 /nobreak >nul

:copy_update
copy /Y "%NEW_EXE%" "%CURRENT_EXE%" >nul
if not errorlevel 1 goto restart_app
set /a COPY_COUNT+=1
if %COPY_COUNT% GEQ 10 goto cleanup_script
timeout /t 1 /nobreak >nul
goto copy_update

:restart_app
rmdir /S /Q "%APP_DIR%.courier_runtime" >nul 2>nul
timeout /t 3 /nobreak >nul
start "" /D "%APP_DIR%" "%CURRENT_EXE%"
del "%NEW_EXE%" >nul 2>nul

:cleanup_script
del "%~f0" >nul 2>nul
"""
        script_path.write_text(script_content, encoding="utf-8")
        return script_path


class CourierApp:
    def __init__(self, root: Any, tk_mod: Any, ttk_mod: Any, messagebox_mod: Any) -> None:
        self.root = root
        self.tk = tk_mod
        self.ttk = ttk_mod
        self.messagebox = messagebox_mod
        self.language_code = DEFAULT_LANGUAGE_CODE
        self.db = MonthlyDatabaseManager(CONFIG_DB_PATH, self.t)
        self.language_code = normalize_language_code(self.db.get_setting("language_code", DEFAULT_LANGUAGE_CODE))
        self.exporter = ReportExporter(self.db, self.t)
        self.backup_manager = BackupManager(self.db, BACKUP_DIR)
        self.update_manager = UpdateManager(APP_DIR, UPDATE_DIR)
        self.selected_company_id: int | None = None
        self.backup_sort_descending = True

        self.root.title(self.window_title())
        self.root.geometry("1220x780")
        self.root.minsize(960, 620)

        self.notebook = self.ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)

        self.scan_tab = self.ttk.Frame(self.notebook)
        self.stats_tab = self.ttk.Frame(self.notebook)
        self.search_tab = self.ttk.Frame(self.notebook)
        self.rules_tab = self.ttk.Frame(self.notebook)
        self.unrecognized_tab = self.ttk.Frame(self.notebook)
        self.duplicates_tab = self.ttk.Frame(self.notebook)
        self.anomalies_tab = self.ttk.Frame(self.notebook)
        self.blacklist_tab = self.ttk.Frame(self.notebook)
        self.archive_tab = self.ttk.Frame(self.notebook)

        self.notebook.add(self.scan_tab, text=self.t("scan_tab"))
        self.notebook.add(self.stats_tab, text=self.t("stats_tab"))
        self.notebook.add(self.search_tab, text=self.t("search_tab"))
        self.notebook.add(self.rules_tab, text=self.t("rules_tab"))
        self.notebook.add(self.unrecognized_tab, text=self.t("unrecognized_tab"))
        self.notebook.add(self.duplicates_tab, text=self.t("duplicates_tab"))
        self.notebook.add(self.anomalies_tab, text=self.t("anomalies_tab"))
        self.notebook.add(self.blacklist_tab, text=self.t("blacklist_tab"))
        self.notebook.add(self.archive_tab, text=self.t("archive_tab"))

        self.operator_var = self.tk.StringVar()
        self.start_date_var = self.tk.StringVar()
        self.end_date_var = self.tk.StringVar()
        self.report_date_var = self.tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self.company_filter_var = self.tk.StringVar()
        self.stats_month_var = self.tk.StringVar(value=ALL_MONTHS_VALUE)
        self.duplicate_policy_var = self.tk.StringVar()
        self.sound_enabled_var = self.tk.StringVar()
        self.block_unrecognized_var = self.tk.BooleanVar(value=False)
        self.backup_search_var = self.tk.StringVar()
        self.blocked_type_var = self.tk.StringVar()
        self.search_month_var = self.tk.StringVar(value=ALL_MONTHS_VALUE)
        self.scan_compact_mode = False

        self._build_scan_tab()
        self._build_stats_tab()
        self._build_search_tab()
        self._build_rules_tab()
        self._build_unrecognized_tab()
        self._build_duplicates_tab()
        self._build_anomalies_tab()
        self._build_blacklist_tab()
        self._build_archive_tab()
        self.load_duplicate_policy_setting()
        self.load_sound_setting()
        self.load_block_unrecognized_setting()
        self.rebuild_operator_shortcut_buttons()
        self.backup_manager.create_daily_auto_backup()
        self.refresh_all_views()
        self.root.bind("<Configure>", self.on_root_resize)
        self.root.after(50, self.update_scan_layout_mode)

    def t(self, key: str, **kwargs: Any) -> str:
        template = TRANSLATIONS.get(self.language_code, TRANSLATIONS["zh"]).get(key, key)
        return template.format(**kwargs) if kwargs else template

    def window_title(self) -> str:
        return f"{self.t('app_title')} v{APP_VERSION}"

    def duplicate_policy_labels(self) -> dict[str, str]:
        return {
            "all": self.t("duplicate_all"),
            "today": self.t("duplicate_today"),
        }

    def duplicate_policy_label_to_value(self, label: str) -> str:
        for value, localized in self.duplicate_policy_labels().items():
            if localized == label:
                return value
        return "all"

    def duplicate_policy_value_to_label(self, value: str) -> str:
        return self.duplicate_policy_labels().get(value, self.t("duplicate_all"))

    def sound_labels(self) -> dict[str, str]:
        return {
            "1": self.t("sound_on"),
            "0": self.t("sound_off"),
        }

    def sound_label_to_value(self, label: str) -> str:
        for value, localized in self.sound_labels().items():
            if localized == label:
                return value
        return "1"

    def sound_value_to_label(self, value: str) -> str:
        return self.sound_labels().get(value, self.t("sound_on"))

    def load_block_unrecognized_setting(self) -> None:
        self.block_unrecognized_var.set(self.db.is_block_unrecognized_enabled())

    def month_filter_options(self) -> list[str]:
        return [self.t("all_months")] + self.db.list_month_keys()

    def month_label_to_value(self, label: str) -> str:
        return ALL_MONTHS_VALUE if label == self.t("all_months") else label

    def month_value_to_label(self, value: str) -> str:
        return self.t("all_months") if not value or value == ALL_MONTHS_VALUE else value

    def anomaly_type_label(self, anomaly_type: str) -> str:
        mapping = {
            "blacklist": self.t("anomaly_blacklist"),
            "locked": self.t("anomaly_locked"),
            "format": self.t("anomaly_format"),
            "unrecognized": self.t("anomaly_unrecognized"),
        }
        return mapping.get(anomaly_type, anomaly_type or self.t("anomaly_unrecognized"))

    def anomaly_note_label(self, note: str) -> str:
        mapping = {
            "tracking_too_short": self.t("note_tracking_too_short"),
            "": self.t("no_notes"),
        }
        return mapping.get(note, note or self.t("no_notes"))

    def intercept_display_style(self, anomaly_type: str) -> tuple[str, str, str, str]:
        if anomaly_type == "blacklist":
            return (
                self.t("intercept_blacklist_title"),
                self.t("intercept_blacklist_big"),
                self.t("intercept_blacklist_result"),
                BLACKLIST_ALERT_COLOR,
            )
        if anomaly_type == "locked":
            return (
                self.t("intercept_locked_title"),
                self.t("intercept_locked_big"),
                self.t("intercept_locked_result"),
                LOCKED_ALERT_COLOR,
            )
        return (
            self.t("anomaly_title"),
            self.t("anomaly_unrecognized"),
            self.t("anomaly_result"),
            "#A61B1B",
        )

    def play_feedback_sound(self, sound_type: str) -> None:
        if not self.db.is_sound_enabled():
            return
        if sys.platform.startswith("win"):
            try:
                import winsound

                sound_map = {
                    "success": (winsound.Beep, (1046, 140)),
                    "duplicate": (winsound.MessageBeep, (winsound.MB_ICONEXCLAMATION,)),
                    "unrecognized": (winsound.Beep, (440, 260)),
                }
                sound_fn, args = sound_map.get(sound_type, (winsound.MessageBeep, (winsound.MB_OK,)))
                sound_fn(*args)
                return
            except Exception:
                pass
        try:
            self.root.bell()
        except Exception:
            pass

    def parse_date_or_warn(self, date_text: str) -> str | None:
        value = date_text.strip()
        if not value:
            return None
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            self.messagebox.showwarning(self.t("warning"), self.t("invalid_date"))
            return None
        return value

    def on_scan_content_configure(self, _event: Any) -> None:
        self.scan_canvas.configure(scrollregion=self.scan_canvas.bbox("all"))

    def on_scan_canvas_configure(self, event: Any) -> None:
        self.scan_canvas.itemconfigure(self.scan_canvas_window, width=event.width)

    def on_scan_mousewheel(self, event: Any) -> None:
        delta = getattr(event, "delta", 0)
        if delta:
            self.scan_canvas.yview_scroll(int(-delta / 120), "units")

    def on_root_resize(self, _event: Any) -> None:
        self.update_scan_layout_mode()

    def update_scan_layout_mode(self) -> None:
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        compact = width < 1120 or height < 760
        if compact == self.scan_compact_mode:
            return
        self.scan_compact_mode = compact

        title_font = ("Microsoft YaHei UI", 16 if compact else 18, "bold")
        desc_font = ("Microsoft YaHei UI", 9 if compact else 10)
        label_font = ("Microsoft YaHei UI", 11 if compact else 12)
        scan_entry_font = ("Consolas", 16 if compact else 20)
        result_font = ("Microsoft YaHei UI", 11 if compact else 12)
        big_company_font = ("Microsoft YaHei UI", 26 if compact else 34, "bold")
        today_total_big_font = ("Microsoft YaHei UI", 24 if compact else 28, "bold")
        metric_value_font = ("Microsoft YaHei UI", 15 if compact else 17, "bold")
        throughput_value_font = ("Microsoft YaHei UI", 14 if compact else 16, "bold")
        card_title_font = ("Microsoft YaHei UI", 10 if compact else 11, "bold")

        self.scan_title_label.config(font=title_font)
        self.scan_desc_label.config(font=desc_font)
        self.scan_tracking_label.config(font=label_font)
        self.scan_entry.config(font=scan_entry_font)
        self.quick_block_text.config(height=1 if compact else 2, font=("Consolas", 10 if compact else 11))
        self.result_label.config(font=result_font)
        self.big_company_label.config(font=big_company_font)
        self.today_total_big_value.config(font=today_total_big_font)
        self.today_total_big_caption.config(font=card_title_font)
        self.hourly_total_label.config(font=card_title_font)
        self.duplicate_today_label.config(font=card_title_font)
        self.throughput_10min_label.config(font=card_title_font)
        self.throughput_1hour_label.config(font=card_title_font)
        self.throughput_avg_label.config(font=card_title_font)
        self.hourly_total_value.config(font=metric_value_font)
        self.duplicate_today_value.config(font=metric_value_font)
        self.throughput_10min_value.config(font=throughput_value_font)
        self.throughput_1hour_value.config(font=throughput_value_font)
        self.throughput_avg_value.config(font=throughput_value_font)

        if compact:
            self.input_frame.grid(row=0, column=0, sticky="ew")
            self.quick_block_recent_frame.grid(row=1, column=0, sticky="ew", padx=0, pady=(10, 0))
            self.scan_entry_area.columnconfigure(0, weight=1)
            self.scan_entry_area.columnconfigure(1, weight=0)
            self.left_panel.grid(row=0, column=0, sticky="nsew", padx=0, pady=(0, 10))
            self.right_panel.grid(row=1, column=0, sticky="nsew", padx=0)
            self.dashboard.columnconfigure(0, weight=1)
            self.dashboard.columnconfigure(1, weight=0)
            self.dashboard.rowconfigure(0, weight=1)
            self.dashboard.rowconfigure(1, weight=0)
        else:
            self.input_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
            self.quick_block_recent_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 0), pady=0)
            self.scan_entry_area.columnconfigure(0, weight=3)
            self.scan_entry_area.columnconfigure(1, weight=1)
            self.left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=0)
            self.right_panel.grid(row=0, column=1, sticky="nsew", padx=0)
            self.dashboard.columnconfigure(0, weight=3)
            self.dashboard.columnconfigure(1, weight=1)
            self.dashboard.rowconfigure(0, weight=1)
            self.dashboard.rowconfigure(1, weight=0)

        self.quick_block_recent_tree.configure(height=3 if compact else 5)
        self.recent_tree.configure(height=12 if compact else 16)
        self.count_tree.configure(height=8 if compact else 12)

    def _build_scan_tab(self) -> None:
        scan_wrapper = self.ttk.Frame(self.scan_tab)
        scan_wrapper.pack(fill="both", expand=True)
        scan_wrapper.columnconfigure(0, weight=1)
        scan_wrapper.rowconfigure(0, weight=1)

        self.scan_canvas = self.tk.Canvas(scan_wrapper, highlightthickness=0)
        self.scan_canvas.grid(row=0, column=0, sticky="nsew")
        scan_scrollbar = self.ttk.Scrollbar(scan_wrapper, orient="vertical", command=self.scan_canvas.yview)
        scan_scrollbar.grid(row=0, column=1, sticky="ns")
        self.scan_canvas.configure(yscrollcommand=scan_scrollbar.set)

        self.scan_content = self.ttk.Frame(self.scan_canvas)
        self.scan_canvas_window = self.scan_canvas.create_window((0, 0), window=self.scan_content, anchor="nw")
        self.scan_content.bind("<Configure>", self.on_scan_content_configure)
        self.scan_canvas.bind("<Configure>", self.on_scan_canvas_configure)
        self.scan_canvas.bind_all("<MouseWheel>", self.on_scan_mousewheel)

        top_frame = self.ttk.Frame(self.scan_content, padding=16)
        top_frame.pack(fill="x")
        top_frame.columnconfigure(0, weight=1)

        self.scan_title_label = self.ttk.Label(top_frame, text=self.t("scan_title"), font=("Microsoft YaHei UI", 18, "bold"))
        self.scan_title_label.grid(row=0, column=0, sticky="w")

        language_frame = self.ttk.Frame(top_frame)
        language_frame.grid(row=0, column=1, sticky="e")
        self.language_label = self.ttk.Label(language_frame, text=self.t("language"))
        self.language_label.grid(row=0, column=0, padx=(0, 8))
        self.language_var = self.tk.StringVar(value=LANGUAGES[self.language_code])
        self.language_combo = self.ttk.Combobox(
            language_frame,
            textvariable=self.language_var,
            state="readonly",
            values=list(LANGUAGES.values()),
            width=18,
        )
        self.language_combo.grid(row=0, column=1)
        self.language_combo.bind("<<ComboboxSelected>>", self.on_language_change)

        self.scan_desc_label = self.ttk.Label(top_frame, text=self.t("scan_desc"), font=("Microsoft YaHei UI", 10))
        self.scan_desc_label.grid(row=1, column=0, sticky="w", pady=(4, 10))

        self.scan_entry_area = self.ttk.Frame(top_frame)
        self.scan_entry_area.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.scan_entry_area.columnconfigure(0, weight=3)
        self.scan_entry_area.columnconfigure(1, weight=1)
        self.scan_entry_area.rowconfigure(0, weight=1)

        self.input_frame = self.ttk.LabelFrame(self.scan_entry_area, text=self.t("scan_frame"), padding=12)
        self.input_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.input_frame.columnconfigure(1, weight=1)
        self.input_frame.columnconfigure(3, weight=1)

        self.scan_tracking_label = self.ttk.Label(self.input_frame, text=self.t("tracking_number"), font=("Microsoft YaHei UI", 12))
        self.scan_tracking_label.grid(row=0, column=0, sticky="w")
        self.scan_entry = self.ttk.Entry(self.input_frame, font=("Consolas", 20))
        self.scan_entry.grid(row=0, column=1, sticky="ew", padx=(10, 10))
        self.scan_entry.bind("<Return>", self.handle_scan_event)

        self.scan_button = self.ttk.Button(self.input_frame, text=self.t("scan_button"), command=self.handle_scan)
        self.scan_button.grid(row=0, column=4, sticky="e")

        self.operator_label = self.ttk.Label(self.input_frame, text=self.t("operator_label"))
        self.operator_label.grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.operator_entry = self.ttk.Entry(self.input_frame, textvariable=self.operator_var)
        self.operator_entry.grid(row=1, column=1, sticky="ew", padx=(10, 10), pady=(12, 0))

        self.operator_shortcut_label = self.ttk.Label(self.input_frame, text=self.t("operator_quick_label"))
        self.operator_shortcut_label.grid(row=2, column=0, sticky="w", pady=(12, 0))

        self.operator_shortcut_frame = self.ttk.Frame(self.input_frame)
        self.operator_shortcut_frame.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(10, 10), pady=(12, 0))

        self.save_operator_shortcut_btn = self.ttk.Button(
            self.input_frame,
            text=self.t("save_operator_shortcut"),
            command=self.save_current_operator_shortcut,
        )
        self.save_operator_shortcut_btn.grid(row=2, column=4, sticky="e", pady=(12, 0))

        self.quick_block_label = self.ttk.Label(self.input_frame, text=self.t("quick_block_label"))
        self.quick_block_label.grid(row=3, column=0, sticky="nw", pady=(12, 0))
        self.quick_block_frame = self.ttk.Frame(self.input_frame)
        self.quick_block_frame.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(10, 10), pady=(12, 0))
        self.quick_block_frame.columnconfigure(0, weight=1)
        self.quick_block_text = self.tk.Text(self.quick_block_frame, height=2, font=("Consolas", 11))
        self.quick_block_text.grid(row=0, column=0, sticky="ew")
        self.quick_block_hint_label = self.ttk.Label(
            self.quick_block_frame,
            text=self.t("quick_block_hint"),
            foreground=UNRECOGNIZED_COLOR,
            justify="left",
        )
        self.quick_block_hint_label.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.quick_block_button_frame = self.ttk.Frame(self.input_frame)
        self.quick_block_button_frame.grid(row=3, column=4, sticky="ne", pady=(12, 0))
        self.quick_block_blacklist_btn = self.ttk.Button(
            self.quick_block_button_frame,
            text=self.t("quick_block_blacklist_button"),
            command=lambda: self.save_quick_block_action("blacklist"),
        )
        self.quick_block_blacklist_btn.grid(row=0, column=0, sticky="ew")
        self.quick_block_lock_btn = self.ttk.Button(
            self.quick_block_button_frame,
            text=self.t("quick_block_lock_button"),
            command=lambda: self.save_quick_block_action("locked"),
        )
        self.quick_block_lock_btn.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.quick_block_recent_frame = self.ttk.LabelFrame(self.scan_entry_area, text=self.t("quick_block_recent_title"), padding=8)
        self.quick_block_recent_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self.quick_block_recent_frame.rowconfigure(0, weight=1)
        self.quick_block_recent_frame.columnconfigure(0, weight=1)
        self.quick_block_recent_tree = self.ttk.Treeview(
            self.quick_block_recent_frame,
            columns=("tracking_number", "entry_type", "created_at"),
            show="headings",
            height=5,
        )
        self.quick_block_recent_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.quick_block_recent_tree.heading("entry_type", text=self.t("entry_type"))
        self.quick_block_recent_tree.heading("created_at", text=self.t("shipped_at"))
        self.quick_block_recent_tree.column("tracking_number", width=180, stretch=False)
        self.quick_block_recent_tree.column("entry_type", width=90, anchor="center", stretch=False)
        self.quick_block_recent_tree.column("created_at", width=150, stretch=False)
        self.quick_block_recent_tree.grid(row=0, column=0, sticky="nsew")

        self.duplicate_policy_label = self.ttk.Label(self.input_frame, text=self.t("duplicate_policy_label"))
        self.duplicate_policy_label.grid(row=4, column=0, sticky="w", pady=(12, 0))
        self.duplicate_policy_combo = self.ttk.Combobox(
            self.input_frame,
            textvariable=self.duplicate_policy_var,
            state="readonly",
            values=list(self.duplicate_policy_labels().values()),
            width=18,
        )
        self.duplicate_policy_combo.grid(row=4, column=1, sticky="ew", pady=(12, 0))
        self.duplicate_policy_combo.bind("<<ComboboxSelected>>", self.on_duplicate_policy_change)

        self.sound_enabled_label = self.ttk.Label(self.input_frame, text=self.t("sound_enabled_label"))
        self.sound_enabled_label.grid(row=4, column=2, sticky="w", padx=(12, 0), pady=(12, 0))
        self.sound_enabled_combo = self.ttk.Combobox(
            self.input_frame,
            textvariable=self.sound_enabled_var,
            state="readonly",
            values=list(self.sound_labels().values()),
            width=10,
        )
        self.sound_enabled_combo.grid(row=4, column=3, sticky="ew", pady=(12, 0))
        self.sound_enabled_combo.bind("<<ComboboxSelected>>", self.on_sound_enabled_change)

        self.block_unrecognized_check = self.ttk.Checkbutton(
            self.input_frame,
            text=self.t("block_unrecognized_label"),
            variable=self.block_unrecognized_var,
            command=self.on_block_unrecognized_change,
        )
        self.block_unrecognized_check.grid(row=5, column=0, columnspan=5, sticky="w", pady=(10, 0))

        self.scan_result_frame = self.ttk.Frame(top_frame)
        self.scan_result_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.scan_result_frame.columnconfigure(0, weight=1)

        self.result_label = self.ttk.Label(
            self.scan_result_frame,
            text=self.t("waiting_scan"),
            font=("Microsoft YaHei UI", 12),
            foreground=DEFAULT_COMPANY_COLOR,
        )
        self.result_label.grid(row=0, column=0, sticky="w")

        self.big_company_label = self.ttk.Label(
            self.scan_result_frame,
            text=self.t("big_company_default"),
            font=("Microsoft YaHei UI", 34, "bold"),
            foreground=UNRECOGNIZED_COLOR,
        )
        self.big_company_label.grid(row=1, column=0, sticky="w", pady=(4, 0))

        self.dashboard = self.ttk.Frame(self.scan_content, padding=(16, 0, 16, 16))
        self.dashboard.pack(fill="both", expand=True)
        self.dashboard.columnconfigure(0, weight=3)
        self.dashboard.columnconfigure(1, weight=1)
        self.dashboard.rowconfigure(0, weight=1)

        self.left_panel = self.ttk.LabelFrame(self.dashboard, text=self.t("recent_records"), padding=12)
        self.left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.left_panel.rowconfigure(0, weight=1)
        self.left_panel.columnconfigure(0, weight=1)

        self.recent_tree = self.ttk.Treeview(
            self.left_panel,
            columns=("tracking_number", "company_name", "operator_name", "shipped_at", "action"),
            show="headings",
            height=16,
        )
        self.recent_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.recent_tree.heading("company_name", text=self.t("company_name"))
        self.recent_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.recent_tree.heading("shipped_at", text=self.t("shipped_at"))
        self.recent_tree.heading("action", text=self.t("action"))
        self.recent_tree.column("tracking_number", width=220)
        self.recent_tree.column("company_name", width=140)
        self.recent_tree.column("operator_name", width=120)
        self.recent_tree.column("shipped_at", width=180)
        self.recent_tree.column("action", width=90, anchor="center")
        self.recent_tree.grid(row=0, column=0, sticky="nsew")
        self.recent_tree.bind("<ButtonRelease-1>", self.handle_recent_tree_click)

        recent_scroll = self.ttk.Scrollbar(self.left_panel, orient="vertical", command=self.recent_tree.yview)
        recent_scroll.grid(row=0, column=1, sticky="ns")
        self.recent_tree.configure(yscrollcommand=recent_scroll.set)

        self.right_panel = self.ttk.LabelFrame(self.dashboard, text=self.t("today_counts"), padding=12)
        self.right_panel.grid(row=0, column=1, sticky="nsew")
        self.right_panel.rowconfigure(2, weight=1)
        self.right_panel.columnconfigure(0, weight=1)

        self.today_total_label = self.ttk.Label(
            self.right_panel,
            text=self.t("today_total", total=0),
            font=("Microsoft YaHei UI", 16, "bold"),
            foreground="#0D6832",
        )
        self.today_total_label.grid(row=0, column=0, sticky="w", pady=(0, 12))

        self.metrics_frame = self.ttk.Frame(self.right_panel)
        self.metrics_frame.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.metrics_frame.columnconfigure(0, weight=1)
        self.metrics_frame.columnconfigure(1, weight=1)

        self.total_card = self.ttk.LabelFrame(self.metrics_frame, padding=10)
        self.total_card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.total_card.columnconfigure(0, weight=1)

        self.today_total_big_caption = self.ttk.Label(
            self.total_card,
            text=self.t("today_total_big_label"),
            font=("Microsoft YaHei UI", 12, "bold"),
            foreground=UNRECOGNIZED_COLOR,
        )
        self.today_total_big_caption.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.today_total_big_value = self.ttk.Label(
            self.total_card,
            text="0",
            font=("Microsoft YaHei UI", 28, "bold"),
            foreground="#0D6832",
        )
        self.today_total_big_value.grid(row=1, column=0, sticky="w")

        self.hourly_card = self.ttk.LabelFrame(self.metrics_frame, padding=10)
        self.hourly_card.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        self.hourly_card.columnconfigure(0, weight=1)

        self.hourly_total_label = self.ttk.Label(
            self.hourly_card,
            text=self.t("hourly_total_label"),
            font=("Microsoft YaHei UI", 11, "bold"),
            foreground=UNRECOGNIZED_COLOR,
        )
        self.hourly_total_label.grid(row=0, column=0, sticky="w", pady=(0, 2))

        self.hourly_total_value = self.ttk.Label(
            self.hourly_card,
            text="0",
            font=("Microsoft YaHei UI", 17, "bold"),
            foreground="#8A5A00",
        )
        self.hourly_total_value.grid(row=1, column=0, sticky="w")

        self.duplicate_card = self.ttk.LabelFrame(self.metrics_frame, padding=10)
        self.duplicate_card.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        self.duplicate_card.columnconfigure(0, weight=1)

        self.duplicate_today_label = self.ttk.Label(
            self.duplicate_card,
            text=self.t("duplicate_today_total_label"),
            font=("Microsoft YaHei UI", 11, "bold"),
            foreground=UNRECOGNIZED_COLOR,
        )
        self.duplicate_today_label.grid(row=0, column=0, sticky="w", pady=(0, 2))

        self.duplicate_today_value = self.ttk.Label(
            self.duplicate_card,
            text="0",
            font=("Microsoft YaHei UI", 17, "bold"),
            foreground="#A61B1B",
        )
        self.duplicate_today_value.grid(row=1, column=0, sticky="w")

        self.throughput_10min_card = self.ttk.LabelFrame(self.metrics_frame, padding=10)
        self.throughput_10min_card.grid(row=2, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        self.throughput_10min_card.columnconfigure(0, weight=1)

        self.throughput_10min_label = self.ttk.Label(
            self.throughput_10min_card,
            text=self.t("throughput_10min"),
            font=("Microsoft YaHei UI", 11, "bold"),
            foreground=UNRECOGNIZED_COLOR,
        )
        self.throughput_10min_label.grid(row=0, column=0, sticky="w", pady=(0, 2))

        self.throughput_10min_value = self.ttk.Label(
            self.throughput_10min_card,
            text="0",
            font=("Microsoft YaHei UI", 16, "bold"),
            foreground="#0B5CAB",
        )
        self.throughput_10min_value.grid(row=1, column=0, sticky="w")

        self.throughput_1hour_card = self.ttk.LabelFrame(self.metrics_frame, padding=10)
        self.throughput_1hour_card.grid(row=2, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        self.throughput_1hour_card.columnconfigure(0, weight=1)

        self.throughput_1hour_label = self.ttk.Label(
            self.throughput_1hour_card,
            text=self.t("throughput_1hour"),
            font=("Microsoft YaHei UI", 11, "bold"),
            foreground=UNRECOGNIZED_COLOR,
        )
        self.throughput_1hour_label.grid(row=0, column=0, sticky="w", pady=(0, 2))

        self.throughput_1hour_value = self.ttk.Label(
            self.throughput_1hour_card,
            text="0",
            font=("Microsoft YaHei UI", 16, "bold"),
            foreground="#5A3E9B",
        )
        self.throughput_1hour_value.grid(row=1, column=0, sticky="w")

        self.throughput_avg_card = self.ttk.LabelFrame(self.metrics_frame, padding=10)
        self.throughput_avg_card.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        self.throughput_avg_card.columnconfigure(0, weight=1)

        self.throughput_avg_label = self.ttk.Label(
            self.throughput_avg_card,
            text=self.t("throughput_avg"),
            font=("Microsoft YaHei UI", 11, "bold"),
            foreground=UNRECOGNIZED_COLOR,
        )
        self.throughput_avg_label.grid(row=0, column=0, sticky="w", pady=(0, 2))

        self.throughput_avg_value = self.ttk.Label(
            self.throughput_avg_card,
            text="0.00",
            font=("Microsoft YaHei UI", 16, "bold"),
            foreground="#9A4D00",
        )
        self.throughput_avg_value.grid(row=1, column=0, sticky="w")

        self.count_tree = self.ttk.Treeview(
            self.right_panel,
            columns=("company_name", "total"),
            show="headings",
            height=12,
        )
        self.count_tree.heading("company_name", text=self.t("company_name"))
        self.count_tree.heading("total", text=self.t("quantity"))
        self.count_tree.column("company_name", width=180)
        self.count_tree.column("total", width=120, anchor="center")
        self.count_tree.grid(row=2, column=0, sticky="nsew")

    def _build_stats_tab(self) -> None:
        container = self.ttk.Frame(self.stats_tab, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(2, weight=0)
        container.rowconfigure(3, weight=1)
        container.rowconfigure(4, weight=1)
        container.rowconfigure(5, weight=1)

        self.stats_header = self.ttk.Label(container, text=self.t("stats_title"), font=("Microsoft YaHei UI", 20, "bold"))
        self.stats_header.grid(row=0, column=0, sticky="w")

        self.stats_action_frame = self.ttk.Frame(container)
        self.stats_action_frame.grid(row=0, column=1, sticky="e")

        self.refresh_btn = self.ttk.Button(self.stats_action_frame, text=self.t("refresh_stats"), command=self.refresh_stats_tab)
        self.refresh_btn.grid(row=0, column=0, padx=(0, 8))

        self.export_btn = self.ttk.Button(self.stats_action_frame, text=self.t("export_excel"), command=self.export_excel_report)
        self.export_btn.grid(row=0, column=1)

        self.backup_btn = self.ttk.Button(self.stats_action_frame, text=self.t("backup_data"), command=self.backup_database)
        self.backup_btn.grid(row=0, column=2, padx=(8, 8))

        self.restore_btn = self.ttk.Button(self.stats_action_frame, text=self.t("restore_data"), command=self.restore_database)
        self.restore_btn.grid(row=0, column=3)

        self.check_update_btn = self.ttk.Button(self.stats_action_frame, text=self.t("check_update"), command=self.check_for_updates)
        self.check_update_btn.grid(row=0, column=4, padx=(8, 8))

        self.update_settings_btn = self.ttk.Button(self.stats_action_frame, text=self.t("update_settings_button"), command=self.open_update_settings)
        self.update_settings_btn.grid(row=0, column=5)

        self.filter_frame = self.ttk.LabelFrame(container, text=self.t("search_frame"), padding=12)
        self.filter_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self.filter_frame.columnconfigure(1, weight=1)
        self.filter_frame.columnconfigure(3, weight=1)
        self.filter_frame.columnconfigure(5, weight=1)
        self.filter_frame.columnconfigure(7, weight=1)

        self.stats_month_label = self.ttk.Label(self.filter_frame, text=self.t("stats_month_label"))
        self.stats_month_label.grid(row=0, column=0, sticky="w")
        self.stats_month_combo = self.ttk.Combobox(self.filter_frame, textvariable=self.stats_month_var, state="readonly")
        self.stats_month_combo.grid(row=0, column=1, sticky="ew", padx=(8, 12))

        self.start_date_label = self.ttk.Label(self.filter_frame, text=self.t("start_date_label"))
        self.start_date_label.grid(row=0, column=2, sticky="w")
        self.start_date_entry = self.ttk.Entry(self.filter_frame, textvariable=self.start_date_var)
        self.start_date_entry.grid(row=0, column=3, sticky="ew", padx=(8, 12))

        self.end_date_label = self.ttk.Label(self.filter_frame, text=self.t("end_date_label"))
        self.end_date_label.grid(row=0, column=4, sticky="w")
        self.end_date_entry = self.ttk.Entry(self.filter_frame, textvariable=self.end_date_var)
        self.end_date_entry.grid(row=0, column=5, sticky="ew", padx=(8, 12))

        self.company_filter_label = self.ttk.Label(self.filter_frame, text=self.t("company_filter_label"))
        self.company_filter_label.grid(row=0, column=6, sticky="w")
        self.company_filter_combo = self.ttk.Combobox(self.filter_frame, textvariable=self.company_filter_var, state="readonly")
        self.company_filter_combo.grid(row=0, column=7, sticky="ew", padx=(8, 12))

        self.apply_filter_btn = self.ttk.Button(self.filter_frame, text=self.t("apply_filter"), command=self.apply_stats_filter)
        self.apply_filter_btn.grid(row=0, column=8, padx=(0, 8))
        self.reset_filter_btn = self.ttk.Button(self.filter_frame, text=self.t("reset_filter"), command=self.reset_stats_filter)
        self.reset_filter_btn.grid(row=0, column=9)

        self.daily_report_frame = self.ttk.LabelFrame(container, text=self.t("daily_report_title"), padding=12)
        self.daily_report_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.daily_report_frame.columnconfigure(1, weight=1)

        self.report_date_label = self.ttk.Label(self.daily_report_frame, text=self.t("report_date_label"))
        self.report_date_label.grid(row=0, column=0, sticky="w")
        self.report_date_entry = self.ttk.Entry(self.daily_report_frame, textvariable=self.report_date_var, width=14)
        self.report_date_entry.grid(row=0, column=1, sticky="w", padx=(8, 8))
        self.report_today_btn = self.ttk.Button(self.daily_report_frame, text=self.t("set_today"), command=self.set_report_date_today)
        self.report_today_btn.grid(row=0, column=2, padx=(0, 8))
        self.report_yesterday_btn = self.ttk.Button(self.daily_report_frame, text=self.t("set_yesterday"), command=self.set_report_date_yesterday)
        self.report_yesterday_btn.grid(row=0, column=3, padx=(0, 16))
        self.export_today_btn = self.ttk.Button(self.daily_report_frame, text=self.t("export_today_report"), command=self.export_today_report)
        self.export_today_btn.grid(row=0, column=4, padx=(0, 8))
        self.export_selected_date_btn = self.ttk.Button(self.daily_report_frame, text=self.t("export_selected_date_report"), command=self.export_selected_date_report)
        self.export_selected_date_btn.grid(row=0, column=5)

        self.daily_frame = self.ttk.LabelFrame(container, text=self.t("daily_stats"), padding=12)
        self.daily_frame.grid(row=3, column=0, sticky="nsew", padx=(0, 8), pady=(16, 0))
        self.daily_frame.rowconfigure(0, weight=1)
        self.daily_frame.columnconfigure(0, weight=1)

        self.daily_tree = self.ttk.Treeview(self.daily_frame, columns=("shipping_day", "total"), show="headings")
        self.daily_tree.heading("shipping_day", text=self.t("date"))
        self.daily_tree.heading("total", text=self.t("package_total"))
        self.daily_tree.column("shipping_day", width=160)
        self.daily_tree.column("total", width=120, anchor="center")
        self.daily_tree.grid(row=0, column=0, sticky="nsew")

        daily_scroll = self.ttk.Scrollbar(self.daily_frame, orient="vertical", command=self.daily_tree.yview)
        daily_scroll.grid(row=0, column=1, sticky="ns")
        self.daily_tree.configure(yscrollcommand=daily_scroll.set)

        self.company_frame = self.ttk.LabelFrame(container, text=self.t("company_totals"), padding=12)
        self.company_frame.grid(row=3, column=1, sticky="nsew", padx=(8, 0), pady=(16, 0))
        self.company_frame.rowconfigure(0, weight=1)
        self.company_frame.columnconfigure(0, weight=1)

        self.stats_company_tree = self.ttk.Treeview(self.company_frame, columns=("company_name", "total"), show="headings")
        self.stats_company_tree.heading("company_name", text=self.t("company_name"))
        self.stats_company_tree.heading("total", text=self.t("cumulative_total"))
        self.stats_company_tree.column("company_name", width=180)
        self.stats_company_tree.column("total", width=120, anchor="center")
        self.stats_company_tree.grid(row=0, column=0, sticky="nsew")

        company_scroll = self.ttk.Scrollbar(self.company_frame, orient="vertical", command=self.stats_company_tree.yview)
        company_scroll.grid(row=0, column=1, sticky="ns")
        self.stats_company_tree.configure(yscrollcommand=company_scroll.set)

        self.operator_frame = self.ttk.LabelFrame(container, text=self.t("operator_totals"), padding=12)
        self.operator_frame.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(16, 0))
        self.operator_frame.rowconfigure(0, weight=1)
        self.operator_frame.columnconfigure(0, weight=1)

        self.operator_tree = self.ttk.Treeview(self.operator_frame, columns=("operator_name", "total"), show="headings")
        self.operator_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.operator_tree.heading("total", text=self.t("quantity"))
        self.operator_tree.column("operator_name", width=220)
        self.operator_tree.column("total", width=120, anchor="center")
        self.operator_tree.grid(row=0, column=0, sticky="nsew")

        operator_scroll = self.ttk.Scrollbar(self.operator_frame, orient="vertical", command=self.operator_tree.yview)
        operator_scroll.grid(row=0, column=1, sticky="ns")
        self.operator_tree.configure(yscrollcommand=operator_scroll.set)

        self.backup_frame = self.ttk.LabelFrame(container, text=self.t("backup_management"), padding=12)
        self.backup_frame.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(16, 0))
        self.backup_frame.rowconfigure(1, weight=1)
        self.backup_frame.columnconfigure(0, weight=1)

        self.backup_action_frame = self.ttk.Frame(self.backup_frame)
        self.backup_action_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        self.backup_search_label = self.ttk.Label(self.backup_action_frame, text=self.t("backup_search_label"))
        self.backup_search_label.grid(row=0, column=0, padx=(0, 8))

        self.backup_search_entry = self.ttk.Entry(self.backup_action_frame, textvariable=self.backup_search_var, width=28)
        self.backup_search_entry.grid(row=0, column=1, padx=(0, 8))
        self.backup_search_entry.bind("<KeyRelease>", self.refresh_backup_list_event)

        self.backup_search_clear_btn = self.ttk.Button(
            self.backup_action_frame,
            text=self.t("backup_search_clear"),
            command=self.clear_backup_search,
        )
        self.backup_search_clear_btn.grid(row=0, column=2, padx=(0, 8))

        self.backup_refresh_btn = self.ttk.Button(
            self.backup_action_frame,
            text=self.t("backup_refresh"),
            command=self.refresh_backup_list,
        )
        self.backup_refresh_btn.grid(row=0, column=3, padx=(0, 8))

        self.backup_sort_btn = self.ttk.Button(
            self.backup_action_frame,
            text=self.t("backup_sort_desc"),
            command=self.toggle_backup_sort,
        )
        self.backup_sort_btn.grid(row=0, column=4, padx=(0, 8))

        self.backup_open_folder_btn = self.ttk.Button(
            self.backup_action_frame,
            text=self.t("backup_open_folder"),
            command=self.open_backup_folder,
        )
        self.backup_open_folder_btn.grid(row=0, column=5, padx=(0, 8))

        self.backup_restore_selected_btn = self.ttk.Button(
            self.backup_action_frame,
            text=self.t("backup_restore_selected"),
            command=self.restore_selected_backup,
        )
        self.backup_restore_selected_btn.grid(row=0, column=6, padx=(0, 8))

        self.backup_delete_selected_btn = self.ttk.Button(
            self.backup_action_frame,
            text=self.t("backup_delete_selected"),
            command=self.delete_selected_backup,
        )
        self.backup_delete_selected_btn.grid(row=0, column=7)

        self.backup_tree = self.ttk.Treeview(
            self.backup_frame,
            columns=("file_name", "created_at", "file_size", "backup_type"),
            show="headings",
        )
        self.backup_tree.heading("file_name", text=self.t("backup_file"))
        self.backup_tree.heading("created_at", text=self.t("backup_created_at"))
        self.backup_tree.heading("file_size", text=self.t("backup_size"))
        self.backup_tree.heading("backup_type", text=self.t("backup_type"))
        self.backup_tree.column("file_name", width=320)
        self.backup_tree.column("created_at", width=180)
        self.backup_tree.column("file_size", width=120, anchor="center")
        self.backup_tree.column("backup_type", width=140, anchor="center")
        self.backup_tree.grid(row=1, column=0, sticky="nsew")

        backup_scroll = self.ttk.Scrollbar(self.backup_frame, orient="vertical", command=self.backup_tree.yview)
        backup_scroll.grid(row=1, column=1, sticky="ns")
        self.backup_tree.configure(yscrollcommand=backup_scroll.set)

        self.backup_empty_label = self.ttk.Label(
            self.backup_frame,
            text=self.t("backup_no_results"),
            foreground=UNRECOGNIZED_COLOR,
        )

    def _build_search_tab(self) -> None:
        container = self.ttk.Frame(self.search_tab, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        self.search_header = self.ttk.Label(container, text=self.t("search_title"), font=("Microsoft YaHei UI", 20, "bold"))
        self.search_header.grid(row=0, column=0, sticky="w")

        self.search_frame = self.ttk.LabelFrame(container, text=self.t("search_frame"), padding=16)
        self.search_frame.grid(row=1, column=0, sticky="ew", pady=(16, 12))
        self.search_frame.columnconfigure(1, weight=1)

        self.search_tracking_label = self.ttk.Label(self.search_frame, text=self.t("tracking_number"), font=("Microsoft YaHei UI", 12))
        self.search_tracking_label.grid(row=0, column=0, sticky="w")
        self.search_entry = self.ttk.Entry(self.search_frame, font=("Consolas", 18))
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(10, 10))
        self.search_entry.bind("<Return>", self.run_tracking_search_event)

        self.search_btn = self.ttk.Button(self.search_frame, text=self.t("search_button"), command=self.run_tracking_search)
        self.search_btn.grid(row=0, column=2, sticky="e")

        self.search_month_label = self.ttk.Label(self.search_frame, text=self.t("search_month_label"))
        self.search_month_label.grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.search_month_combo = self.ttk.Combobox(self.search_frame, textvariable=self.search_month_var, state="readonly")
        self.search_month_combo.grid(row=1, column=1, sticky="w", padx=(10, 10), pady=(12, 0))

        self.search_status = self.ttk.Label(self.search_frame, text=self.t("search_hint"), foreground=UNRECOGNIZED_COLOR)
        self.search_status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))

        self.result_frame = self.ttk.LabelFrame(container, text=self.t("search_results"), padding=12)
        self.result_frame.grid(row=2, column=0, sticky="nsew")
        self.result_frame.rowconfigure(0, weight=1)
        self.result_frame.columnconfigure(0, weight=1)

        self.search_tree = self.ttk.Treeview(
            self.result_frame,
            columns=("tracking_number", "company_name", "operator_name", "shipped_at", "shipping_day"),
            show="headings",
        )
        self.search_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.search_tree.heading("company_name", text=self.t("company_name"))
        self.search_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.search_tree.heading("shipped_at", text=self.t("shipped_at"))
        self.search_tree.heading("shipping_day", text=self.t("shipping_day"))
        self.search_tree.column("tracking_number", width=220)
        self.search_tree.column("company_name", width=140)
        self.search_tree.column("operator_name", width=120)
        self.search_tree.column("shipped_at", width=180)
        self.search_tree.column("shipping_day", width=120)
        self.search_tree.grid(row=0, column=0, sticky="nsew")

        search_scroll = self.ttk.Scrollbar(self.result_frame, orient="vertical", command=self.search_tree.yview)
        search_scroll.grid(row=0, column=1, sticky="ns")
        self.search_tree.configure(yscrollcommand=search_scroll.set)

    def _build_rules_tab(self) -> None:
        container = self.ttk.Frame(self.rules_tab, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=2)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(0, weight=1)

        self.rules_left = self.ttk.LabelFrame(container, text=self.t("rules_title"), padding=12)
        self.rules_left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.rules_left.rowconfigure(0, weight=1)
        self.rules_left.columnconfigure(0, weight=1)

        self.rules_tree = self.ttk.Treeview(self.rules_left, columns=("id", "name", "prefix", "color"), show="headings")
        self.rules_tree.heading("id", text=self.t("id"))
        self.rules_tree.heading("name", text=self.t("company_name"))
        self.rules_tree.heading("prefix", text=self.t("prefix_label").rstrip(":"))
        self.rules_tree.heading("color", text=self.t("company_color_label").rstrip(":"))
        self.rules_tree.column("id", width=70, anchor="center")
        self.rules_tree.column("name", width=200)
        self.rules_tree.column("prefix", width=200)
        self.rules_tree.column("color", width=120, anchor="center")
        self.rules_tree.grid(row=0, column=0, sticky="nsew")
        self.rules_tree.bind("<<TreeviewSelect>>", self.on_rule_select)

        rules_scroll = self.ttk.Scrollbar(self.rules_left, orient="vertical", command=self.rules_tree.yview)
        rules_scroll.grid(row=0, column=1, sticky="ns")
        self.rules_tree.configure(yscrollcommand=rules_scroll.set)

        self.rules_right = self.ttk.LabelFrame(container, text=self.t("rules_editor"), padding=16)
        self.rules_right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.rules_right.columnconfigure(1, weight=1)

        self.company_name_label = self.ttk.Label(self.rules_right, text=self.t("company_name_label"))
        self.company_name_label.grid(row=0, column=0, sticky="w")
        self.company_name_entry = self.ttk.Entry(self.rules_right)
        self.company_name_entry.grid(row=0, column=1, sticky="ew", pady=(0, 12))

        self.prefix_label = self.ttk.Label(self.rules_right, text=self.t("prefix_label"))
        self.prefix_label.grid(row=1, column=0, sticky="w")
        self.company_prefix_entry = self.ttk.Entry(self.rules_right)
        self.company_prefix_entry.grid(row=1, column=1, sticky="ew", pady=(0, 12))

        self.company_color_label = self.ttk.Label(self.rules_right, text=self.t("company_color_label"))
        self.company_color_label.grid(row=2, column=0, sticky="w")
        self.company_color_entry = self.ttk.Entry(self.rules_right)
        self.company_color_entry.grid(row=2, column=1, sticky="ew", pady=(0, 12))
        self.company_color_entry.insert(0, DEFAULT_COMPANY_COLOR)

        self.choose_color_btn = self.ttk.Button(self.rules_right, text=self.t("choose_color"), command=self.choose_color)
        self.choose_color_btn.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        self.rules_helper_label = self.ttk.Label(self.rules_right, text=self.t("rules_helper"), foreground=UNRECOGNIZED_COLOR, justify="left")
        self.rules_helper_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 16))

        self.rule_test_label = self.ttk.Label(self.rules_right, text=self.t("test_rule_label"))
        self.rule_test_label.grid(row=5, column=0, sticky="w")
        self.rule_test_entry = self.ttk.Entry(self.rules_right)
        self.rule_test_entry.grid(row=5, column=1, sticky="ew", pady=(0, 12))
        self.rule_test_entry.bind("<Return>", self.test_rule_match_event)

        self.rule_test_btn = self.ttk.Button(self.rules_right, text=self.t("test_rule_button"), command=self.test_rule_match)
        self.rule_test_btn.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        self.rule_test_result = self.ttk.Label(
            self.rules_right,
            text=self.t("test_rule_result_default"),
            foreground=UNRECOGNIZED_COLOR,
            justify="left",
        )
        self.rule_test_result.grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self.save_btn = self.ttk.Button(self.rules_right, text=self.t("save_rule"), command=self.save_rule)
        self.save_btn.grid(row=8, column=0, sticky="ew", pady=(0, 8))

        self.clear_btn = self.ttk.Button(self.rules_right, text=self.t("clear_form"), command=self.clear_rule_form)
        self.clear_btn.grid(row=8, column=1, sticky="ew", pady=(0, 8))

        self.delete_btn = self.ttk.Button(self.rules_right, text=self.t("delete_rule"), command=self.delete_rule)
        self.delete_btn.grid(row=9, column=0, columnspan=2, sticky="ew")

        self.rule_status = self.ttk.Label(self.rules_right, text=self.t("rules_status_default"), foreground=UNRECOGNIZED_COLOR)
        self.rule_status.grid(row=10, column=0, columnspan=2, sticky="w", pady=(16, 0))

    def _build_unrecognized_tab(self) -> None:
        container = self.ttk.Frame(self.unrecognized_tab, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        self.unrecognized_header = self.ttk.Label(container, text=self.t("unrecognized_title"), font=("Microsoft YaHei UI", 20, "bold"))
        self.unrecognized_header.grid(row=0, column=0, sticky="w")

        self.unrecognized_desc_label = self.ttk.Label(container, text=self.t("unrecognized_desc"), foreground=UNRECOGNIZED_COLOR)
        self.unrecognized_desc_label.grid(row=1, column=0, sticky="w", pady=(8, 16))

        self.unrecognized_count_label = self.ttk.Label(container, text=self.t("unrecognized_count", total=0), foreground="#A61B1B")
        self.unrecognized_count_label.grid(row=1, column=0, sticky="e")

        self.unrecognized_frame = self.ttk.LabelFrame(container, text=self.t("unrecognized_tab"), padding=12)
        self.unrecognized_frame.grid(row=2, column=0, sticky="nsew")
        self.unrecognized_frame.rowconfigure(0, weight=1)
        self.unrecognized_frame.columnconfigure(0, weight=1)

        self.unrecognized_tree = self.ttk.Treeview(
            self.unrecognized_frame,
            columns=("tracking_number", "operator_name", "scanned_at"),
            show="headings",
        )
        self.unrecognized_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.unrecognized_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.unrecognized_tree.heading("scanned_at", text=self.t("shipped_at"))
        self.unrecognized_tree.column("tracking_number", width=260)
        self.unrecognized_tree.column("operator_name", width=140)
        self.unrecognized_tree.column("scanned_at", width=200)
        self.unrecognized_tree.grid(row=0, column=0, sticky="nsew")

        unrecognized_scroll = self.ttk.Scrollbar(self.unrecognized_frame, orient="vertical", command=self.unrecognized_tree.yview)
        unrecognized_scroll.grid(row=0, column=1, sticky="ns")
        self.unrecognized_tree.configure(yscrollcommand=unrecognized_scroll.set)

    def _build_duplicates_tab(self) -> None:
        container = self.ttk.Frame(self.duplicates_tab, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1)

        self.duplicates_header = self.ttk.Label(container, text=self.t("duplicate_records_title"), font=("Microsoft YaHei UI", 20, "bold"))
        self.duplicates_header.grid(row=0, column=0, sticky="w")

        self.duplicates_desc_label = self.ttk.Label(container, text=self.t("duplicate_records_desc"), foreground=UNRECOGNIZED_COLOR)
        self.duplicates_desc_label.grid(row=1, column=0, sticky="w", pady=(8, 16))

        self.duplicates_count_label = self.ttk.Label(container, text=self.t("duplicate_records_count", total=0), foreground="#A61B1B")
        self.duplicates_count_label.grid(row=1, column=0, sticky="e")

        self.duplicate_reason_frame = self.ttk.Frame(container)
        self.duplicate_reason_frame.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.duplicate_reason_label = self.ttk.Label(self.duplicate_reason_frame, text=self.t("duplicate_reason"))
        self.duplicate_reason_label.grid(row=0, column=0, padx=(0, 8))
        self.duplicate_reason_entry = self.ttk.Entry(self.duplicate_reason_frame, width=40)
        self.duplicate_reason_entry.grid(row=0, column=1, padx=(0, 8))
        self.save_duplicate_reason_btn = self.ttk.Button(
            self.duplicate_reason_frame,
            text=self.t("save_duplicate_reason"),
            command=self.save_duplicate_reason_action,
        )
        self.save_duplicate_reason_btn.grid(row=0, column=2)

        self.duplicates_frame = self.ttk.LabelFrame(container, text=self.t("duplicates_tab"), padding=12)
        self.duplicates_frame.grid(row=3, column=0, sticky="nsew")
        self.duplicates_frame.rowconfigure(0, weight=1)
        self.duplicates_frame.columnconfigure(0, weight=1)

        self.duplicates_tree = self.ttk.Treeview(
            self.duplicates_frame,
            columns=("tracking_number", "company_name", "operator_name", "duplicate_at", "last_seen_at", "reason_note"),
            show="headings",
        )
        self.duplicates_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.duplicates_tree.heading("company_name", text=self.t("company_name"))
        self.duplicates_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.duplicates_tree.heading("duplicate_at", text=self.t("shipped_at"))
        self.duplicates_tree.heading("last_seen_at", text=self.t("last_seen_time"))
        self.duplicates_tree.heading("reason_note", text=self.t("duplicate_reason"))
        self.duplicates_tree.column("tracking_number", width=220)
        self.duplicates_tree.column("company_name", width=150)
        self.duplicates_tree.column("operator_name", width=120)
        self.duplicates_tree.column("duplicate_at", width=180)
        self.duplicates_tree.column("last_seen_at", width=180)
        self.duplicates_tree.column("reason_note", width=220)
        self.duplicates_tree.grid(row=0, column=0, sticky="nsew")

        duplicates_scroll = self.ttk.Scrollbar(self.duplicates_frame, orient="vertical", command=self.duplicates_tree.yview)
        duplicates_scroll.grid(row=0, column=1, sticky="ns")
        self.duplicates_tree.configure(yscrollcommand=duplicates_scroll.set)
        self.duplicates_tree.bind("<<TreeviewSelect>>", self.on_duplicate_select)

    def _build_anomalies_tab(self) -> None:
        container = self.ttk.Frame(self.anomalies_tab, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        self.anomalies_header = self.ttk.Label(container, text=self.t("anomalies_title"), font=("Microsoft YaHei UI", 20, "bold"))
        self.anomalies_header.grid(row=0, column=0, sticky="w")

        self.anomalies_desc_label = self.ttk.Label(container, text=self.t("anomalies_desc"), foreground=UNRECOGNIZED_COLOR)
        self.anomalies_desc_label.grid(row=1, column=0, sticky="w", pady=(8, 16))

        self.anomalies_count_label = self.ttk.Label(container, text=self.t("anomalies_count", total=0), foreground="#A61B1B")
        self.anomalies_count_label.grid(row=1, column=0, sticky="e")

        self.anomalies_frame = self.ttk.LabelFrame(container, text=self.t("anomalies_tab"), padding=12)
        self.anomalies_frame.grid(row=2, column=0, sticky="nsew")
        self.anomalies_frame.rowconfigure(0, weight=1)
        self.anomalies_frame.columnconfigure(0, weight=1)

        self.anomalies_tree = self.ttk.Treeview(
            self.anomalies_frame,
            columns=("tracking_number", "operator_name", "anomaly_type", "notes", "created_at"),
            show="headings",
        )
        self.anomalies_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.anomalies_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.anomalies_tree.heading("anomaly_type", text=self.t("anomaly_type"))
        self.anomalies_tree.heading("notes", text=self.t("notes"))
        self.anomalies_tree.heading("created_at", text=self.t("shipped_at"))
        self.anomalies_tree.column("tracking_number", width=220)
        self.anomalies_tree.column("operator_name", width=120)
        self.anomalies_tree.column("anomaly_type", width=140)
        self.anomalies_tree.column("notes", width=200)
        self.anomalies_tree.column("created_at", width=180)
        self.anomalies_tree.grid(row=0, column=0, sticky="nsew")

        anomalies_scroll = self.ttk.Scrollbar(self.anomalies_frame, orient="vertical", command=self.anomalies_tree.yview)
        anomalies_scroll.grid(row=0, column=1, sticky="ns")
        self.anomalies_tree.configure(yscrollcommand=anomalies_scroll.set)

    def _build_blacklist_tab(self) -> None:
        container = self.ttk.Frame(self.blacklist_tab, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        self.blacklist_header = self.ttk.Label(container, text=self.t("blacklist_title"), font=("Microsoft YaHei UI", 20, "bold"))
        self.blacklist_header.grid(row=0, column=0, sticky="w")

        self.blacklist_desc_label = self.ttk.Label(container, text=self.t("blacklist_desc"), foreground=UNRECOGNIZED_COLOR)
        self.blacklist_desc_label.grid(row=1, column=0, sticky="w", pady=(8, 16))

        self.blacklist_editor = self.ttk.Frame(container)
        self.blacklist_editor.grid(row=1, column=0, sticky="e")

        self.blocked_tracking_entry = self.ttk.Entry(self.blacklist_editor, width=24)
        self.blocked_tracking_entry.grid(row=0, column=0, padx=(0, 8))
        self.blocked_type_combo = self.ttk.Combobox(
            self.blacklist_editor,
            textvariable=self.blocked_type_var,
            state="readonly",
            values=[self.t("blacklist_type"), self.t("lock_type")],
            width=12,
        )
        self.blocked_type_combo.grid(row=0, column=1, padx=(0, 8))
        self.blocked_type_var.set(self.t("blacklist_type"))
        self.blocked_notes_entry = self.ttk.Entry(self.blacklist_editor, width=24)
        self.blocked_notes_entry.grid(row=0, column=2, padx=(0, 8))
        self.save_blocked_btn = self.ttk.Button(self.blacklist_editor, text=self.t("save_blocked_tracking"), command=self.save_blocked_tracking_action)
        self.save_blocked_btn.grid(row=0, column=3, padx=(0, 8))
        self.delete_blocked_btn = self.ttk.Button(self.blacklist_editor, text=self.t("delete_blocked_tracking"), command=self.delete_blocked_tracking_action)
        self.delete_blocked_btn.grid(row=0, column=4)

        self.blacklist_frame = self.ttk.LabelFrame(container, text=self.t("blacklist_tab"), padding=12)
        self.blacklist_frame.grid(row=2, column=0, sticky="nsew")
        self.blacklist_frame.rowconfigure(0, weight=1)
        self.blacklist_frame.columnconfigure(0, weight=1)

        self.blacklist_tree = self.ttk.Treeview(
            self.blacklist_frame,
            columns=("id", "tracking_number", "entry_type", "notes", "created_at"),
            show="headings",
        )
        self.blacklist_tree.heading("id", text=self.t("id"))
        self.blacklist_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.blacklist_tree.heading("entry_type", text=self.t("entry_type"))
        self.blacklist_tree.heading("notes", text=self.t("notes"))
        self.blacklist_tree.heading("created_at", text=self.t("shipped_at"))
        self.blacklist_tree.column("id", width=70, anchor="center")
        self.blacklist_tree.column("tracking_number", width=220)
        self.blacklist_tree.column("entry_type", width=120)
        self.blacklist_tree.column("notes", width=220)
        self.blacklist_tree.column("created_at", width=180)
        self.blacklist_tree.grid(row=0, column=0, sticky="nsew")

        blacklist_scroll = self.ttk.Scrollbar(self.blacklist_frame, orient="vertical", command=self.blacklist_tree.yview)
        blacklist_scroll.grid(row=0, column=1, sticky="ns")
        self.blacklist_tree.configure(yscrollcommand=blacklist_scroll.set)

    def _build_archive_tab(self) -> None:
        container = self.ttk.Frame(self.archive_tab, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)

        self.archive_header = self.ttk.Label(container, text=self.t("archive_title"), font=("Microsoft YaHei UI", 20, "bold"))
        self.archive_header.grid(row=0, column=0, sticky="w")

        self.archive_desc_label = self.ttk.Label(container, text=self.t("archive_desc"), foreground=UNRECOGNIZED_COLOR)
        self.archive_desc_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 16))

        self.archive_date_label = self.ttk.Label(container, text=self.t("archive_before_date"))
        self.archive_date_label.grid(row=2, column=0, sticky="w")
        self.archive_date_entry = self.ttk.Entry(container)
        self.archive_date_entry.grid(row=2, column=1, sticky="ew", padx=(8, 12))
        self.archive_btn = self.ttk.Button(container, text=self.t("archive_button"), command=self.archive_old_data_action)
        self.archive_btn.grid(row=2, column=2, sticky="e")

    def choose_color(self) -> None:
        from tkinter import colorchooser

        chosen, _hex_value = colorchooser.askcolor(color=self.company_color_entry.get().strip() or DEFAULT_COMPANY_COLOR)
        if not chosen:
            return
        rgb_hex = "#%02x%02x%02x" % tuple(int(channel) for channel in chosen)
        self.company_color_entry.delete(0, self.tk.END)
        self.company_color_entry.insert(0, rgb_hex.upper())

    def test_rule_match_event(self, _event: Any) -> None:
        self.test_rule_match()

    def test_rule_match(self) -> None:
        tracking_number = self.rule_test_entry.get().strip()
        if not tracking_number:
            self.rule_test_result.config(text=self.t("test_rule_result_default"), foreground=UNRECOGNIZED_COLOR)
            return
        _company_id, company_name, color = self.db.resolve_company(tracking_number)
        self.rule_test_result.config(text=self.t("test_rule_result", company=company_name, color=color), foreground=color)

    def load_duplicate_policy_setting(self) -> None:
        value = self.db.get_duplicate_policy()
        self.duplicate_policy_var.set(self.duplicate_policy_value_to_label(value))

    def load_sound_setting(self) -> None:
        value = "1" if self.db.is_sound_enabled() else "0"
        self.sound_enabled_var.set(self.sound_value_to_label(value))

    def parse_bulk_tracking_numbers(self, raw_text: str) -> list[str]:
        tokens = re.split(r"[\s,;，；]+", raw_text.strip().upper())
        seen: set[str] = set()
        results: list[str] = []
        for token in tokens:
            if not token or token in seen:
                continue
            seen.add(token)
            results.append(token)
        return results

    def rebuild_operator_shortcut_buttons(self) -> None:
        for child in self.operator_shortcut_frame.winfo_children():
            child.destroy()
        shortcuts = self.db.get_operator_shortcuts()
        for index, operator_name in enumerate(shortcuts):
            btn = self.ttk.Button(
                self.operator_shortcut_frame,
                text=operator_name,
                command=lambda value=operator_name: self.use_operator_shortcut(value),
            )
            btn.grid(row=0, column=index * 2, padx=(0, 4))
            remove_btn = self.ttk.Button(
                self.operator_shortcut_frame,
                text=self.t("remove"),
                width=6,
                command=lambda value=operator_name: self.delete_operator_shortcut_action(value),
            )
            remove_btn.grid(row=0, column=index * 2 + 1, padx=(0, 8))

    def use_operator_shortcut(self, operator_name: str) -> None:
        self.operator_var.set(operator_name)
        self.scan_entry.focus_set()

    def save_current_operator_shortcut(self) -> None:
        operator_name = self.operator_var.get().strip()
        if not operator_name:
            self.messagebox.showwarning(self.t("warning"), self.t("operator_shortcut_empty"))
            return
        self.db.save_operator_shortcut(operator_name)
        self.rebuild_operator_shortcut_buttons()
        self.result_label.config(text=self.t("operator_shortcut_saved", name=operator_name), foreground="#0D6832")

    def save_quick_block_action(self, entry_type: str) -> None:
        tracking_numbers = self.parse_bulk_tracking_numbers(self.quick_block_text.get("1.0", self.tk.END))
        if not tracking_numbers:
            self.messagebox.showwarning(self.t("warning"), self.t("quick_block_empty"))
            return
        for tracking_number in tracking_numbers:
            self.db.upsert_blocked_tracking_number(tracking_number, entry_type, "")
        self.quick_block_text.delete("1.0", self.tk.END)
        self.refresh_blocked_tracking_tab()
        message_key = "quick_block_saved_blacklist" if entry_type == "blacklist" else "quick_block_saved_locked"
        result_color = BLACKLIST_ALERT_COLOR if entry_type == "blacklist" else LOCKED_ALERT_COLOR
        self.result_label.config(text=self.t(message_key, count=len(tracking_numbers)), foreground=result_color)

    def delete_operator_shortcut_action(self, operator_name: str) -> None:
        should_delete = self.messagebox.askyesno(self.t("warning"), self.t("operator_shortcut_delete_confirm"))
        if not should_delete:
            return
        self.db.delete_operator_shortcut(operator_name)
        self.rebuild_operator_shortcut_buttons()

    def on_duplicate_select(self, _event: Any) -> None:
        selection = self.duplicates_tree.selection()
        if not selection:
            return
        values = self.duplicates_tree.item(selection[0], "values")
        if len(values) >= 6:
            self.duplicate_reason_entry.delete(0, self.tk.END)
            self.duplicate_reason_entry.insert(0, values[5] or "")

    def save_duplicate_reason_action(self) -> None:
        selection = self.duplicates_tree.selection()
        if not selection:
            self.messagebox.showwarning(self.t("warning"), self.t("duplicate_select_record"))
            return
        reason_note = self.duplicate_reason_entry.get().strip()
        if not reason_note:
            self.messagebox.showwarning(self.t("warning"), self.t("duplicate_reason_empty"))
            return
        event_id = int(selection[0])
        self.db.update_duplicate_reason(event_id, reason_note)
        self.refresh_duplicates_tab()
        self.result_label.config(text=self.t("duplicate_reason_saved"), foreground="#0D6832")

    def save_blocked_tracking_action(self) -> None:
        tracking_number = self.blocked_tracking_entry.get().strip().upper()
        entry_type_label = self.blocked_type_var.get().strip()
        notes = self.blocked_notes_entry.get().strip()
        if not tracking_number or not entry_type_label:
            self.messagebox.showwarning(self.t("warning"), self.t("blocked_tracking_empty"))
            return
        entry_type = "blacklist" if entry_type_label == self.t("blacklist_type") else "locked"
        self.db.upsert_blocked_tracking_number(tracking_number, entry_type, notes)
        self.blocked_tracking_entry.delete(0, self.tk.END)
        self.blocked_notes_entry.delete(0, self.tk.END)
        self.refresh_blocked_tracking_tab()
        self.result_label.config(text=self.t("blocked_tracking_saved", tracking=tracking_number), foreground="#0D6832")

    def delete_blocked_tracking_action(self) -> None:
        selection = self.blacklist_tree.selection()
        if not selection:
            self.messagebox.showwarning(self.t("warning"), self.t("blocked_select_delete"))
            return
        blocked_id = int(self.blacklist_tree.item(selection[0], "values")[0])
        self.db.delete_blocked_tracking_number(blocked_id)
        self.refresh_blocked_tracking_tab()
        self.result_label.config(text=self.t("blocked_tracking_deleted"), foreground="#A61B1B")

    def archive_old_data_action(self) -> None:
        cutoff_day = self.parse_date_or_warn(self.archive_date_entry.get())
        if self.archive_date_entry.get().strip() and cutoff_day is None:
            return
        if not cutoff_day:
            self.messagebox.showwarning(self.t("warning"), self.t("invalid_date"))
            return
        should_archive = self.messagebox.askyesno(self.t("archive_title"), self.t("archive_confirm"))
        if not should_archive:
            return
        try:
            archive_path = self.db.archive_old_data(cutoff_day, ARCHIVE_DIR)
        except (OSError, sqlite3.Error) as exc:
            self.messagebox.showerror(self.t("archive_failed"), self.t("archive_failed_message", error=exc))
            return
        if archive_path is None:
            self.messagebox.showwarning(self.t("warning"), self.t("archive_no_rows"))
            return
        self.refresh_all_views()
        self.messagebox.showinfo(self.t("archive_success"), self.t("archive_success_message", path=archive_path))

    def refresh_company_filter_options(self) -> None:
        options = [self.t("all_companies")] + self.db.get_company_filter_names()
        current_value = self.company_filter_var.get().strip()
        self.company_filter_combo.configure(values=options)
        if current_value in options:
            self.company_filter_var.set(current_value)
        else:
            self.company_filter_var.set(self.t("all_companies"))

    def refresh_month_filter_options(self) -> None:
        options = self.month_filter_options()
        stats_value = self.stats_month_var.get().strip()
        search_value = self.search_month_var.get().strip()
        self.stats_month_combo.configure(values=options)
        self.search_month_combo.configure(values=options)
        self.stats_month_var.set(stats_value if stats_value in options else self.t("all_months"))
        self.search_month_var.set(search_value if search_value in options else self.t("all_months"))

    def on_duplicate_policy_change(self, _event: Any) -> None:
        self.db.set_duplicate_policy(self.duplicate_policy_label_to_value(self.duplicate_policy_var.get()))

    def on_sound_enabled_change(self, _event: Any) -> None:
        self.db.set_sound_enabled(self.sound_label_to_value(self.sound_enabled_var.get()) == "1")

    def on_block_unrecognized_change(self) -> None:
        self.db.set_block_unrecognized_enabled(bool(self.block_unrecognized_var.get()))

    def on_language_change(self, _event: Any) -> None:
        selected_label = self.language_var.get()
        for code, label in LANGUAGES.items():
            if label == selected_label:
                self.language_code = code
                break
        self.db.set_setting("language_code", self.language_code)
        self.apply_language()

    def apply_language(self) -> None:
        self.root.title(self.window_title())
        self.language_label.config(text=self.t("language"))
        self.language_combo.configure(values=list(LANGUAGES.values()))
        self.language_var.set(LANGUAGES[self.language_code])
        self.notebook.tab(0, text=self.t("scan_tab"))
        self.notebook.tab(1, text=self.t("stats_tab"))
        self.notebook.tab(2, text=self.t("search_tab"))
        self.notebook.tab(3, text=self.t("rules_tab"))
        self.notebook.tab(4, text=self.t("unrecognized_tab"))
        self.notebook.tab(5, text=self.t("duplicates_tab"))
        self.notebook.tab(6, text=self.t("anomalies_tab"))
        self.notebook.tab(7, text=self.t("blacklist_tab"))
        self.notebook.tab(8, text=self.t("archive_tab"))

        self.scan_title_label.config(text=self.t("scan_title"))
        self.scan_desc_label.config(text=self.t("scan_desc"))
        self.input_frame.config(text=self.t("scan_frame"))
        self.scan_tracking_label.config(text=self.t("tracking_number"))
        self.operator_label.config(text=self.t("operator_label"))
        self.operator_shortcut_label.config(text=self.t("operator_quick_label"))
        self.save_operator_shortcut_btn.config(text=self.t("save_operator_shortcut"))
        self.quick_block_label.config(text=self.t("quick_block_label"))
        self.quick_block_hint_label.config(text=self.t("quick_block_hint"))
        self.quick_block_blacklist_btn.config(text=self.t("quick_block_blacklist_button"))
        self.quick_block_lock_btn.config(text=self.t("quick_block_lock_button"))
        self.quick_block_recent_frame.config(text=self.t("quick_block_recent_title"))
        self.quick_block_recent_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.quick_block_recent_tree.heading("entry_type", text=self.t("entry_type"))
        self.quick_block_recent_tree.heading("created_at", text=self.t("shipped_at"))
        self.duplicate_policy_label.config(text=self.t("duplicate_policy_label"))
        self.duplicate_policy_combo.configure(values=list(self.duplicate_policy_labels().values()))
        self.load_duplicate_policy_setting()
        self.sound_enabled_label.config(text=self.t("sound_enabled_label"))
        self.sound_enabled_combo.configure(values=list(self.sound_labels().values()))
        self.load_sound_setting()
        self.block_unrecognized_check.config(text=self.t("block_unrecognized_label"))
        self.load_block_unrecognized_setting()
        self.scan_button.config(text=self.t("scan_button"))
        self.result_label.config(text=self.t("waiting_scan"))
        self.big_company_label.config(text=self.t("big_company_default"), foreground=UNRECOGNIZED_COLOR)
        self.left_panel.config(text=self.t("recent_records"))
        self.right_panel.config(text=self.t("today_counts"))
        self.today_total_big_caption.config(text=self.t("today_total_big_label"))
        self.hourly_total_label.config(text=self.t("hourly_total_label"))
        self.duplicate_today_label.config(text=self.t("duplicate_today_total_label"))
        self.throughput_10min_label.config(text=self.t("throughput_10min"))
        self.throughput_1hour_label.config(text=self.t("throughput_1hour"))
        self.throughput_avg_label.config(text=self.t("throughput_avg"))
        self.recent_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.recent_tree.heading("company_name", text=self.t("company_name"))
        self.recent_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.recent_tree.heading("shipped_at", text=self.t("shipped_at"))
        self.recent_tree.heading("action", text=self.t("action"))
        self.count_tree.heading("company_name", text=self.t("company_name"))
        self.count_tree.heading("total", text=self.t("quantity"))

        self.stats_header.config(text=self.t("stats_title"))
        self.refresh_btn.config(text=self.t("refresh_stats"))
        self.export_btn.config(text=self.t("export_excel"))
        self.backup_btn.config(text=self.t("backup_data"))
        self.restore_btn.config(text=self.t("restore_data"))
        self.check_update_btn.config(text=self.t("check_update"))
        self.update_settings_btn.config(text=self.t("update_settings_button"))
        self.filter_frame.config(text=self.t("search_frame"))
        self.stats_month_label.config(text=self.t("stats_month_label"))
        self.start_date_label.config(text=self.t("start_date_label"))
        self.end_date_label.config(text=self.t("end_date_label"))
        self.company_filter_label.config(text=self.t("company_filter_label"))
        self.apply_filter_btn.config(text=self.t("apply_filter"))
        self.reset_filter_btn.config(text=self.t("reset_filter"))
        self.daily_report_frame.config(text=self.t("daily_report_title"))
        self.report_date_label.config(text=self.t("report_date_label"))
        self.report_today_btn.config(text=self.t("set_today"))
        self.report_yesterday_btn.config(text=self.t("set_yesterday"))
        self.export_today_btn.config(text=self.t("export_today_report"))
        self.export_selected_date_btn.config(text=self.t("export_selected_date_report"))
        self.refresh_month_filter_options()
        self.refresh_company_filter_options()
        self.daily_frame.config(text=self.t("daily_stats"))
        self.company_frame.config(text=self.t("company_totals"))
        self.operator_frame.config(text=self.t("operator_totals"))
        self.backup_frame.config(text=self.t("backup_management"))
        self.backup_search_label.config(text=self.t("backup_search_label"))
        self.backup_search_clear_btn.config(text=self.t("backup_search_clear"))
        self.backup_refresh_btn.config(text=self.t("backup_refresh"))
        self.backup_sort_btn.config(text=self.t("backup_sort_desc") if self.backup_sort_descending else self.t("backup_sort_asc"))
        self.backup_open_folder_btn.config(text=self.t("backup_open_folder"))
        self.backup_restore_selected_btn.config(text=self.t("backup_restore_selected"))
        self.backup_delete_selected_btn.config(text=self.t("backup_delete_selected"))
        self.backup_empty_label.config(text=self.t("backup_no_results"))
        self.daily_tree.heading("shipping_day", text=self.t("date"))
        self.daily_tree.heading("total", text=self.t("package_total"))
        self.stats_company_tree.heading("company_name", text=self.t("company_name"))
        self.stats_company_tree.heading("total", text=self.t("cumulative_total"))
        self.operator_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.operator_tree.heading("total", text=self.t("quantity"))
        self.backup_tree.heading("file_name", text=self.t("backup_file"))
        self.backup_tree.heading("created_at", text=self.t("backup_created_at"))
        self.backup_tree.heading("file_size", text=self.t("backup_size"))
        self.backup_tree.heading("backup_type", text=self.t("backup_type"))

        self.search_header.config(text=self.t("search_title"))
        self.search_frame.config(text=self.t("search_frame"))
        self.search_tracking_label.config(text=self.t("tracking_number"))
        self.search_month_label.config(text=self.t("search_month_label"))
        self.search_btn.config(text=self.t("search_button"))
        self.search_status.config(text=self.t("search_hint"), foreground=UNRECOGNIZED_COLOR)
        self.result_frame.config(text=self.t("search_results"))
        self.search_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.search_tree.heading("company_name", text=self.t("company_name"))
        self.search_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.search_tree.heading("shipped_at", text=self.t("shipped_at"))
        self.search_tree.heading("shipping_day", text=self.t("shipping_day"))

        self.rules_left.config(text=self.t("rules_title"))
        self.rules_right.config(text=self.t("rules_editor"))
        self.company_name_label.config(text=self.t("company_name_label"))
        self.prefix_label.config(text=self.t("prefix_label"))
        self.company_color_label.config(text=self.t("company_color_label"))
        self.choose_color_btn.config(text=self.t("choose_color"))
        self.rules_helper_label.config(text=self.t("rules_helper"))
        self.rule_test_label.config(text=self.t("test_rule_label"))
        self.rule_test_btn.config(text=self.t("test_rule_button"))
        self.rule_test_result.config(text=self.t("test_rule_result_default"), foreground=UNRECOGNIZED_COLOR)
        self.save_btn.config(text=self.t("save_rule"))
        self.clear_btn.config(text=self.t("clear_form"))
        self.delete_btn.config(text=self.t("delete_rule"))
        self.rule_status.config(text=self.t("rules_status_default"), foreground=UNRECOGNIZED_COLOR)
        self.rules_tree.heading("id", text=self.t("id"))
        self.rules_tree.heading("name", text=self.t("company_name"))
        self.rules_tree.heading("prefix", text=self.t("prefix_label").rstrip(":"))
        self.rules_tree.heading("color", text=self.t("company_color_label").rstrip(":"))

        self.unrecognized_header.config(text=self.t("unrecognized_title"))
        self.unrecognized_desc_label.config(text=self.t("unrecognized_desc"))
        self.unrecognized_frame.config(text=self.t("unrecognized_tab"))
        self.unrecognized_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.unrecognized_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.unrecognized_tree.heading("scanned_at", text=self.t("shipped_at"))

        self.duplicates_header.config(text=self.t("duplicate_records_title"))
        self.duplicates_desc_label.config(text=self.t("duplicate_records_desc"))
        self.duplicate_reason_label.config(text=self.t("duplicate_reason"))
        self.save_duplicate_reason_btn.config(text=self.t("save_duplicate_reason"))
        self.duplicates_frame.config(text=self.t("duplicates_tab"))
        self.duplicates_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.duplicates_tree.heading("company_name", text=self.t("company_name"))
        self.duplicates_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.duplicates_tree.heading("duplicate_at", text=self.t("shipped_at"))
        self.duplicates_tree.heading("last_seen_at", text=self.t("last_seen_time"))
        self.duplicates_tree.heading("reason_note", text=self.t("duplicate_reason"))

        self.anomalies_header.config(text=self.t("anomalies_title"))
        self.anomalies_desc_label.config(text=self.t("anomalies_desc"))
        self.anomalies_frame.config(text=self.t("anomalies_tab"))
        self.anomalies_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.anomalies_tree.heading("operator_name", text=self.t("operator_label").rstrip(":"))
        self.anomalies_tree.heading("anomaly_type", text=self.t("anomaly_type"))
        self.anomalies_tree.heading("notes", text=self.t("notes"))
        self.anomalies_tree.heading("created_at", text=self.t("shipped_at"))

        self.blacklist_header.config(text=self.t("blacklist_title"))
        self.blacklist_desc_label.config(text=self.t("blacklist_desc"))
        self.blocked_type_combo.configure(values=[self.t("blacklist_type"), self.t("lock_type")])
        if self.blocked_type_var.get() not in [self.t("blacklist_type"), self.t("lock_type")]:
            self.blocked_type_var.set(self.t("blacklist_type"))
        self.save_blocked_btn.config(text=self.t("save_blocked_tracking"))
        self.delete_blocked_btn.config(text=self.t("delete_blocked_tracking"))
        self.blacklist_frame.config(text=self.t("blacklist_tab"))
        self.blacklist_tree.heading("id", text=self.t("id"))
        self.blacklist_tree.heading("tracking_number", text=self.t("tracking_number").rstrip(":"))
        self.blacklist_tree.heading("entry_type", text=self.t("entry_type"))
        self.blacklist_tree.heading("notes", text=self.t("notes"))
        self.blacklist_tree.heading("created_at", text=self.t("shipped_at"))

        self.archive_header.config(text=self.t("archive_title"))
        self.archive_desc_label.config(text=self.t("archive_desc"))
        self.archive_date_label.config(text=self.t("archive_before_date"))
        self.archive_btn.config(text=self.t("archive_button"))

        self.refresh_all_views()
        self.rebuild_operator_shortcut_buttons()

    def clear_tree(self, tree: Any) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def refresh_scan_tab(self) -> None:
        today_total = self.db.get_today_total()
        current_hour_total = self.db.get_current_hour_total()
        duplicate_today_total = self.db.get_today_duplicate_total()
        throughput = self.db.get_throughput_metrics()
        self.today_total_label.config(text=self.t("today_total", total=today_total))
        self.today_total_big_value.config(text=str(today_total))
        self.hourly_total_value.config(text=str(current_hour_total))
        self.duplicate_today_value.config(text=str(duplicate_today_total))
        self.throughput_10min_value.config(text=str(throughput["last_10_min"]))
        self.throughput_1hour_value.config(text=str(throughput["last_1_hour"]))
        self.throughput_avg_value.config(text=f'{throughput["avg_per_min"]:.2f}')
        self.rebuild_operator_shortcut_buttons()

        self.clear_tree(self.count_tree)
        for row in self.db.get_today_company_counts():
            self.count_tree.insert("", "end", values=(row["company_name"], row["total"]))

        self.clear_tree(self.recent_tree)
        for row in self.db.get_recent_shipments():
            display_operator = row["operator_name"] if row["operator_name"] else "-"
            self.recent_tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(row["tracking_number"], row["company_name"], display_operator, row["shipped_at"], self.t("delete")),
            )

        self.clear_tree(self.quick_block_recent_tree)
        for row in self.db.get_blocked_tracking_numbers()[:5]:
            entry_type_label = self.t("blacklist_type") if row["entry_type"] == "blacklist" else self.t("lock_type")
            self.quick_block_recent_tree.insert(
                "",
                "end",
                values=(row["tracking_number"], entry_type_label, row["created_at"]),
            )

    def refresh_stats_tab(self) -> None:
        start_day = self.start_date_var.get().strip() or None
        end_day = self.end_date_var.get().strip() or None
        selected_company = self.company_filter_var.get().strip()
        company_name = None if not selected_company or selected_company == self.t("all_companies") else selected_company
        selected_month = self.month_label_to_value(self.stats_month_var.get().strip())

        self.clear_tree(self.daily_tree)
        for row in self.db.get_daily_stats(start_day, end_day, company_name, selected_month):
            self.daily_tree.insert("", "end", values=(row["shipping_day"], row["total"]))

        self.clear_tree(self.stats_company_tree)
        for row in self.db.get_company_stats(start_day, end_day, company_name, selected_month):
            self.stats_company_tree.insert("", "end", values=(row["company_name"], row["total"]))

        self.clear_tree(self.operator_tree)
        for row in self.db.get_operator_stats(start_day, end_day, company_name, selected_month):
            self.operator_tree.insert("", "end", values=(row["operator_name"], row["total"]))

        self.refresh_backup_list()

    def refresh_rules_tab(self) -> None:
        self.clear_tree(self.rules_tree)
        for row in self.db.get_company_rules():
            self.rules_tree.insert("", "end", values=(row["id"], row["name"], row["prefix"], row["color"]))

    def refresh_unrecognized_tab(self) -> None:
        rows = self.db.get_unrecognized_shipments()
        self.unrecognized_count_label.config(text=self.t("unrecognized_count", total=len(rows)))
        self.clear_tree(self.unrecognized_tree)
        for row in rows:
            display_operator = row["operator_name"] if row["operator_name"] else "-"
            self.unrecognized_tree.insert("", "end", values=(row["tracking_number"], display_operator, row["scanned_at"]))

    def refresh_duplicates_tab(self) -> None:
        rows = self.db.get_today_duplicate_events()
        self.duplicates_count_label.config(text=self.t("duplicate_records_count", total=len(rows)))
        self.clear_tree(self.duplicates_tree)
        for row in rows:
            display_operator = row["operator_name"] if row["operator_name"] else "-"
            self.duplicates_tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(row["tracking_number"], row["company_name"], display_operator, row["duplicate_at"], row["last_seen_at"], row["reason_note"]),
            )

    def refresh_anomalies_tab(self) -> None:
        rows = self.db.get_today_anomaly_events()
        self.anomalies_count_label.config(text=self.t("anomalies_count", total=len(rows)))
        self.clear_tree(self.anomalies_tree)
        for row in rows:
            display_operator = row["operator_name"] if row["operator_name"] else "-"
            self.anomalies_tree.insert(
                "",
                "end",
                values=(
                    row["tracking_number"],
                    display_operator,
                    self.anomaly_type_label(row["anomaly_type"]),
                    self.anomaly_note_label(row["notes"]),
                    row["created_at"],
                ),
            )

    def refresh_blocked_tracking_tab(self) -> None:
        rows = self.db.get_blocked_tracking_numbers()
        self.clear_tree(self.blacklist_tree)
        for row in rows:
            entry_type_label = self.t("blacklist_type") if row["entry_type"] == "blacklist" else self.t("lock_type")
            self.blacklist_tree.insert(
                "",
                "end",
                values=(row["id"], row["tracking_number"], entry_type_label, row["notes"], row["created_at"]),
            )

    def refresh_all_views(self) -> None:
        self.refresh_scan_tab()
        self.refresh_month_filter_options()
        self.refresh_company_filter_options()
        self.refresh_stats_tab()
        self.refresh_rules_tab()
        self.refresh_unrecognized_tab()
        self.refresh_duplicates_tab()
        self.refresh_anomalies_tab()
        self.refresh_blocked_tracking_tab()
        self.scan_entry.focus_set()

    def handle_scan_event(self, _event: Any) -> None:
        self.handle_scan()

    def handle_scan(self) -> None:
        tracking_number = self.scan_entry.get().strip()
        operator_name = self.operator_var.get().strip()
        if not tracking_number:
            self.messagebox.showwarning(self.t("warning"), self.t("scan_empty"))
            return

        saved, last_shipment, shipment = self.db.save_shipment_if_new(tracking_number, operator_name)
        if not saved and last_shipment:
            self.play_feedback_sound("duplicate")
            warning_message = self.t(
                "duplicate_message",
                tracking=shipment["tracking_number"],
                time=last_shipment["shipped_at"],
                company=last_shipment["company_name"],
            )
            self.messagebox.showwarning(self.t("duplicate_title"), warning_message)
            self.result_label.config(
                text=self.t("duplicate_result", tracking=shipment["tracking_number"], time=last_shipment["shipped_at"]),
                foreground="#A61B1B",
            )
            self.big_company_label.config(text=last_shipment["company_name"], foreground=shipment["company_color"])
        elif not saved and shipment.get("anomaly_type"):
            self.play_feedback_sound("unrecognized")
            anomaly_type_label = self.anomaly_type_label(shipment["anomaly_type"])
            anomaly_note = self.anomaly_note_label(shipment.get("notes", ""))
            if shipment["anomaly_type"] == "blacklist":
                dialog_title, big_text, result_template, color = self.intercept_display_style("blacklist")
                dialog_message = self.t(
                    "intercept_blacklist_message",
                    tracking=shipment["tracking_number"],
                    notes=anomaly_note,
                )
            elif shipment["anomaly_type"] == "locked":
                dialog_title, big_text, result_template, color = self.intercept_display_style("locked")
                dialog_message = self.t(
                    "intercept_locked_message",
                    tracking=shipment["tracking_number"],
                    notes=anomaly_note,
                )
            else:
                dialog_title, big_text, result_template, color = self.intercept_display_style(shipment["anomaly_type"])
                dialog_message = self.t(
                    "anomaly_message",
                    tracking=shipment["tracking_number"],
                    type=anomaly_type_label,
                    notes=anomaly_note,
                )
            self.messagebox.showwarning(dialog_title, dialog_message)
            self.result_label.config(text=result_template.format(tracking=shipment["tracking_number"], type=anomaly_type_label), foreground=color)
            self.big_company_label.config(text=big_text, foreground=color)
        else:
            if shipment["company_name"] == self.t("unrecognized"):
                self.play_feedback_sound("unrecognized")
            else:
                self.play_feedback_sound("success")
            self.result_label.config(
                text=self.t(
                    "save_success",
                    tracking=shipment["tracking_number"],
                    company=shipment["company_name"],
                    time=shipment["shipped_at"],
                ),
                foreground="#0D6832",
            )
            self.big_company_label.config(text=shipment["company_name"], foreground=shipment["company_color"])

        self.scan_entry.delete(0, self.tk.END)
        self.refresh_all_views()
        self.scan_entry.focus_set()

    def export_excel_report(self) -> None:
        start_day = self.parse_date_or_warn(self.start_date_var.get())
        end_day = self.parse_date_or_warn(self.end_date_var.get())
        if self.start_date_var.get().strip() and start_day is None:
            return
        if self.end_date_var.get().strip() and end_day is None:
            return
        selected_company = self.company_filter_var.get().strip()
        company_name = None if not selected_company or selected_company == self.t("all_companies") else selected_company
        selected_month = self.month_label_to_value(self.stats_month_var.get().strip())

        try:
            summary_path, detail_path = self.exporter.export_report(EXPORT_DIR, start_day, end_day, company_name, selected_month)
        except OSError as exc:
            self.messagebox.showerror(self.t("export_failed"), self.t("export_failed_message", error=exc))
            return

        self.messagebox.showinfo(
            self.t("export_success"),
            self.t("export_success_message", summary_path=summary_path, detail_path=detail_path),
        )

    def set_report_date_today(self) -> None:
        self.report_date_var.set(datetime.now().strftime("%Y-%m-%d"))

    def set_report_date_yesterday(self) -> None:
        self.report_date_var.set((datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"))

    def export_today_report(self) -> None:
        report_day = datetime.now().strftime("%Y-%m-%d")
        self.report_date_var.set(report_day)
        self.export_daily_report(report_day)

    def export_selected_date_report(self) -> None:
        if not self.report_date_var.get().strip():
            self.messagebox.showwarning(self.t("warning"), self.t("invalid_date"))
            return
        report_day = self.parse_date_or_warn(self.report_date_var.get())
        if report_day is None:
            return
        self.export_daily_report(report_day)

    def export_daily_report(self, report_day: str) -> None:
        selected_month = month_key_from_date(report_day)
        report_tag = report_day.replace("-", "")
        try:
            summary_path, detail_path = self.exporter.export_report(
                EXPORT_DIR,
                report_day,
                report_day,
                None,
                selected_month,
                report_tag=report_tag,
            )
        except OSError as exc:
            self.messagebox.showerror(self.t("export_failed"), self.t("export_failed_message", error=exc))
            return

        self.messagebox.showinfo(
            self.t("export_success"),
            self.t("export_success_message", summary_path=summary_path, detail_path=detail_path),
        )

    def reconnect_database(self) -> None:
        self.db = MonthlyDatabaseManager(CONFIG_DB_PATH, self.t)
        self.exporter = ReportExporter(self.db, self.t)
        self.backup_manager = BackupManager(self.db, BACKUP_DIR)

    def format_backup_size(self, size_bytes: int) -> str:
        if size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        if size_bytes >= 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes} B"

    def backup_type_label(self, backup_name: str) -> str:
        if backup_name.startswith(AUTO_BACKUP_PREFIX):
            return self.t("backup_type_auto")
        if backup_name.startswith(MANUAL_BACKUP_PREFIX):
            return self.t("backup_type_manual")
        if backup_name.startswith(PRE_RESTORE_BACKUP_PREFIX):
            return self.t("backup_type_pre_restore")
        return self.t("backup_type_other")

    def toggle_backup_sort(self) -> None:
        self.backup_sort_descending = not self.backup_sort_descending
        self.backup_sort_btn.config(text=self.t("backup_sort_desc") if self.backup_sort_descending else self.t("backup_sort_asc"))
        self.refresh_backup_list()

    def refresh_backup_list_event(self, _event: Any) -> None:
        self.refresh_backup_list()

    def clear_backup_search(self) -> None:
        self.backup_search_var.set("")
        self.refresh_backup_list()

    def open_update_settings(self) -> None:
        selector = self.tk.Toplevel(self.root)
        selector.title(self.t("update_settings_title"))
        selector.geometry("720x180")
        selector.transient(self.root)
        selector.grab_set()

        frame = self.ttk.Frame(selector, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        manifest_label = self.ttk.Label(frame, text=self.t("update_manifest_label"))
        manifest_label.grid(row=0, column=0, sticky="w")
        manifest_var = self.tk.StringVar(value=self.db.get_setting("update_manifest_url", ""))
        manifest_entry = self.ttk.Entry(frame, textvariable=manifest_var)
        manifest_entry.grid(row=1, column=0, sticky="ew", pady=(8, 12))

        def save_manifest_url() -> None:
            self.db.set_setting("update_manifest_url", manifest_var.get().strip())
            self.result_label.config(text=self.t("update_settings_saved"), foreground="#0D6832")
            selector.destroy()

        save_btn = self.ttk.Button(frame, text=self.t("save_rule"), command=save_manifest_url)
        save_btn.grid(row=2, column=0, sticky="e")

    def check_for_updates(self) -> None:
        if not self.update_manager.is_supported_environment():
            self.messagebox.showwarning(self.t("warning"), self.t("update_not_supported"))
            return

        manifest_url = self.db.get_setting("update_manifest_url", "").strip()
        if not manifest_url:
            self.messagebox.showwarning(self.t("warning"), self.t("update_manifest_missing"))
            return

        try:
            manifest = self.update_manager.fetch_manifest(manifest_url)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.messagebox.showerror(self.t("warning"), self.t("update_check_failed", error=exc))
            return

        latest_version = str(manifest.get("version", "")).strip()
        download_url = str(manifest.get("download_url", "")).strip()
        sha256_value = str(manifest.get("sha256", "")).strip() or None
        if not latest_version or not download_url:
            self.messagebox.showerror(self.t("warning"), self.t("update_invalid_manifest"))
            return
        if not self.update_manager.is_newer_version(latest_version, APP_VERSION):
            self.messagebox.showinfo(self.t("check_update"), self.t("update_latest"))
            return

        should_update = self.messagebox.askyesno(
            self.t("update_available_title"),
            self.t("update_available_message", current=APP_VERSION, latest=latest_version),
        )
        if not should_update:
            return

        try:
            self.backup_manager.create_backup(PRE_UPDATE_BACKUP_PREFIX)
        except OSError as exc:
            self.messagebox.showerror(self.t("warning"), self.t("update_backup_failed", error=exc))
            return

        try:
            downloaded_path = self.update_manager.download_update_package(download_url, sha256_value)
            current_exe = Path(sys.executable)
            script_path = self.update_manager.create_windows_upgrade_script(current_exe, downloaded_path, os.getpid())
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(["cmd", "/c", str(script_path)], creationflags=creation_flags)
        except (OSError, ValueError) as exc:
            self.messagebox.showerror(self.t("warning"), self.t("update_download_failed", error=exc))
            return

        self.force_exit_for_update()

    def force_exit_for_update(self) -> None:
        try:
            self.db.close()
        except (sqlite3.Error, OSError):
            pass
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass
        os._exit(0)

    def open_backup_folder(self) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(BACKUP_DIR))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(BACKUP_DIR)], check=True)
            else:
                subprocess.run(["xdg-open", str(BACKUP_DIR)], check=True)
        except Exception as exc:
            self.messagebox.showerror(self.t("backup_failed"), self.t("backup_open_folder_failed", error=exc))

    def refresh_backup_list(self) -> None:
        self.clear_tree(self.backup_tree)
        search_term = self.backup_search_var.get().strip().lower()
        backup_files = sorted(
            self.backup_manager.list_backup_files(),
            key=lambda path: path.stat().st_mtime,
            reverse=self.backup_sort_descending,
        )
        for backup_path in backup_files:
            stat = backup_path.stat()
            created_at = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            backup_type = self.backup_type_label(backup_path.name)
            searchable_text = f"{backup_path.name} {created_at} {backup_type}".lower()
            if search_term and search_term not in searchable_text:
                continue
            self.backup_tree.insert(
                "",
                "end",
                iid=str(backup_path),
                values=(
                    backup_path.name,
                    created_at,
                    self.format_backup_size(stat.st_size),
                    backup_type,
                ),
            )
        if self.backup_tree.get_children():
            self.backup_empty_label.grid_remove()
        else:
            self.backup_empty_label.grid(row=2, column=0, sticky="w", pady=(10, 0))

    def get_selected_backup_path(self) -> Path | None:
        selection = self.backup_tree.selection()
        if not selection:
            self.messagebox.showwarning(self.t("warning"), self.t("backup_no_selection"))
            return None
        return Path(selection[0])

    def backup_database(self) -> None:
        try:
            backup_path = self.backup_manager.create_backup()
        except OSError as exc:
            self.messagebox.showerror(self.t("backup_failed"), self.t("backup_failed_message", error=exc))
            return
        self.refresh_backup_list()
        self.messagebox.showinfo(self.t("backup_success"), self.t("backup_success_message", path=backup_path))

    def restore_database(self) -> None:
        backup_files = self.backup_manager.list_backup_files()
        if not backup_files:
            self.messagebox.showwarning(self.t("warning"), self.t("restore_no_backup"))
            return

        backup_path = self.select_backup_file(backup_files)
        if not backup_path:
            return

        self.restore_from_backup_path(backup_path)

    def restore_from_backup_path(self, backup_path: Path) -> None:
        should_restore = self.messagebox.askyesno(self.t("restore_confirm_title"), self.t("restore_confirm_message"))
        if not should_restore:
            return

        try:
            self.backup_manager.create_backup(PRE_RESTORE_BACKUP_PREFIX)
            self.db.close()
            with tempfile.TemporaryDirectory(dir=str(BACKUP_DIR)) as temp_dir_str:
                temp_dir = Path(temp_dir_str)
                with zipfile.ZipFile(backup_path, "r") as archive_file:
                    archive_file.extractall(temp_dir)

                replacement_paths: dict[str, Path] = {}
                config_path = temp_dir / CONFIG_DB_NAME
                if config_path.exists():
                    temp_config = Database(config_path, self.t, mode="config")
                    temp_config.close()
                    replacement_paths[CONFIG_DB_NAME] = config_path

                for month_path in temp_dir.glob(f"{MONTH_DB_PREFIX}*.db"):
                    month_key = month_key_from_db_path(month_path)
                    if not month_key:
                        continue
                    temp_month = Database(month_path, self.t, mode="monthly")
                    temp_month.close()
                    replacement_paths[month_path.name] = month_path

                if not replacement_paths:
                    raise OSError("backup archive does not contain any database files")

                self.db.replace_databases(replacement_paths)
            self.reconnect_database()
        except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            self.reconnect_database()
            self.messagebox.showerror(self.t("restore_failed"), self.t("restore_failed_message", error=exc))
            return

        self.result_label.config(text=self.t("restore_success_message", path=backup_path), foreground="#0D6832")
        self.refresh_all_views()
        self.messagebox.showinfo(self.t("restore_success"), self.t("restore_success_message", path=backup_path))

    def restore_selected_backup(self) -> None:
        backup_path = self.get_selected_backup_path()
        if not backup_path:
            return
        self.restore_from_backup_path(backup_path)

    def delete_selected_backup(self) -> None:
        backup_path = self.get_selected_backup_path()
        if not backup_path:
            return

        should_delete = self.messagebox.askyesno(
            self.t("backup_delete_confirm_title"),
            self.t("backup_delete_confirm_message"),
        )
        if not should_delete:
            return

        try:
            self.backup_manager.delete_backup(backup_path)
        except OSError as exc:
            self.messagebox.showerror(self.t("backup_failed"), self.t("backup_delete_failed", error=exc))
            return

        self.refresh_backup_list()
        self.result_label.config(text=self.t("backup_delete_success", path=backup_path.name), foreground="#A61B1B")

    def select_backup_file(self, backup_files: list[Path]) -> Path | None:
        selector = self.tk.Toplevel(self.root)
        selector.title(self.t("restore_select_title"))
        selector.geometry("720x420")
        selector.transient(self.root)
        selector.grab_set()

        frame = self.ttk.Frame(selector, padding=16)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        listbox = self.tk.Listbox(frame, font=("Consolas", 11))
        listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = self.ttk.Scrollbar(frame, orient="vertical", command=listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        listbox.configure(yscrollcommand=scrollbar.set)

        for backup_path in backup_files:
            listbox.insert(self.tk.END, backup_path.name)
        if backup_files:
            listbox.selection_set(0)

        result: dict[str, Path | None] = {"path": None}

        def confirm_selection() -> None:
            selection = listbox.curselection()
            if not selection:
                return
            result["path"] = backup_files[selection[0]]
            selector.destroy()

        def cancel_selection() -> None:
            selector.destroy()

        actions = self.ttk.Frame(frame)
        actions.grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        confirm_btn = self.ttk.Button(actions, text=self.t("restore_data"), command=confirm_selection)
        confirm_btn.grid(row=0, column=0, padx=(0, 8))
        cancel_btn = self.ttk.Button(actions, text=self.t("cancel"), command=cancel_selection)
        cancel_btn.grid(row=0, column=1)

        listbox.bind("<Double-Button-1>", lambda _event: confirm_selection())
        selector.wait_window()
        return result["path"]

    def apply_stats_filter(self) -> None:
        start_day = self.parse_date_or_warn(self.start_date_var.get())
        end_day = self.parse_date_or_warn(self.end_date_var.get())
        if self.start_date_var.get().strip() and start_day is None:
            return
        if self.end_date_var.get().strip() and end_day is None:
            return
        self.refresh_stats_tab()

    def reset_stats_filter(self) -> None:
        self.start_date_var.set("")
        self.end_date_var.set("")
        self.stats_month_var.set(self.t("all_months"))
        self.company_filter_var.set(self.t("all_companies"))
        self.refresh_stats_tab()

    def run_tracking_search_event(self, _event: Any) -> None:
        self.run_tracking_search()

    def run_tracking_search(self) -> None:
        tracking_number = self.search_entry.get().strip()
        if not tracking_number:
            self.messagebox.showwarning(self.t("warning"), self.t("search_empty"))
            return

        selected_month = self.month_label_to_value(self.search_month_var.get().strip())
        rows = self.db.query_tracking_number(tracking_number, selected_month)
        self.clear_tree(self.search_tree)
        if not rows:
            self.search_status.config(text=self.t("search_not_found", tracking=tracking_number.upper()), foreground="#A61B1B")
            return

        for row in rows:
            display_operator = row["operator_name"] if row["operator_name"] else "-"
            self.search_tree.insert(
                "",
                "end",
                values=(row["tracking_number"], row["company_name"], display_operator, row["shipped_at"], row["shipping_day"]),
            )
        self.search_status.config(text=self.t("search_found", count=len(rows), time=rows[0]["shipped_at"]), foreground="#0D6832")

    def on_rule_select(self, _event: Any) -> None:
        selection = self.rules_tree.selection()
        if not selection:
            return
        values = self.rules_tree.item(selection[0], "values")
        self.selected_company_id = int(values[0])
        self.company_name_entry.delete(0, self.tk.END)
        self.company_name_entry.insert(0, values[1])
        self.company_prefix_entry.delete(0, self.tk.END)
        self.company_prefix_entry.insert(0, values[2])
        self.company_color_entry.delete(0, self.tk.END)
        self.company_color_entry.insert(0, values[3])

    def clear_rule_form(self) -> None:
        self.selected_company_id = None
        self.company_name_entry.delete(0, self.tk.END)
        self.company_prefix_entry.delete(0, self.tk.END)
        self.company_color_entry.delete(0, self.tk.END)
        self.company_color_entry.insert(0, DEFAULT_COMPANY_COLOR)
        self.rules_tree.selection_remove(self.rules_tree.selection())
        self.rule_status.config(text=self.t("rule_cleared"), foreground=UNRECOGNIZED_COLOR)

    def save_rule(self) -> None:
        name = self.company_name_entry.get().strip()
        prefix = self.company_prefix_entry.get().strip().upper()
        color = self.company_color_entry.get().strip().upper()

        if not name or not prefix:
            self.messagebox.showwarning(self.t("warning"), self.t("rule_empty"))
            return
        if not color:
            self.messagebox.showwarning(self.t("warning"), self.t("color_empty"))
            return

        try:
            self.db.upsert_company(self.selected_company_id, name, prefix, color)
        except sqlite3.IntegrityError:
            self.messagebox.showerror(self.t("rule_save_failed"), self.t("rule_save_failed_message"))
            return

        reprocessed = self.db.reprocess_unrecognized_shipments()
        if reprocessed:
            self.rule_status.config(text=self.t("rule_saved_reprocessed", name=name, prefix=prefix, count=reprocessed), foreground="#0D6832")
        else:
            self.rule_status.config(text=self.t("rule_saved", name=name, prefix=prefix), foreground="#0D6832")
        self.clear_rule_form()
        self.refresh_all_views()

    def delete_rule(self) -> None:
        if not self.selected_company_id:
            self.messagebox.showwarning(self.t("warning"), self.t("rule_select_delete"))
            return

        should_delete = self.messagebox.askyesno(self.t("delete_confirm_title"), self.t("delete_confirm_message"))
        if not should_delete:
            return

        self.db.delete_company(self.selected_company_id)
        self.rule_status.config(text=self.t("rule_deleted"), foreground="#A61B1B")
        self.clear_rule_form()
        self.refresh_all_views()

    def handle_recent_tree_click(self, event: Any) -> None:
        item_id = self.recent_tree.identify_row(event.y)
        column_id = self.recent_tree.identify_column(event.x)
        if not item_id or column_id != "#5":
            return

        should_delete = self.messagebox.askyesno(self.t("delete_record_title"), self.t("delete_record_message"))
        if not should_delete:
            return

        self.db.delete_shipment(int(item_id))
        self.result_label.config(text=self.t("record_deleted"), foreground="#A61B1B")
        self.refresh_scan_tab()
        self.refresh_stats_tab()
        self.refresh_unrecognized_tab()


def main() -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    ensure_storage_ready()
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    app = CourierApp(root, tk, ttk, messagebox)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.scan_canvas.unbind_all("<MouseWheel>"), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
