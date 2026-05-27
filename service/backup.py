import shutil
from pathlib import Path
from datetime import datetime, timezone
from typing import List
from astrbot.api import logger
from storage.database import SQLiteDB


class BackupService:
    def __init__(self, db: SQLiteDB, backup_dir: Path):
        self.db = db
        self.backup_dir = backup_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    async def create_backup(self) -> Path:
        """创建数据库备份"""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"memory.db.bak.{timestamp}"
        await self.db.vacuum_backup(backup_path)
        logger.debug(f"备份已创建: {backup_path}")
        return backup_path

    def list_backups(self) -> List[Path]:
        """列出所有备份文件"""
        if not self.backup_dir.exists():
            return []
        backups = sorted(
            [p for p in self.backup_dir.iterdir() if p.suffix.startswith(".bak")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return backups

    def get_latest_backup(self) -> Path:
        backups = self.list_backups()
        if not backups:
            raise FileNotFoundError("没有可用的备份")
        return backups[0]

    async def restore_latest(self) -> Path:
        """从最新备份恢复"""
        latest = self.get_latest_backup()
        # 先备份当前数据库
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        pre_restore = self.backup_dir / f"memory.db.pre_restore.{timestamp}"
        shutil.copy2(self.db.db_path, pre_restore)
        # 恢复
        shutil.copy2(latest, self.db.db_path)
        logger.info(f"已从备份恢复: {latest}")
        return latest

    def cleanup_old_backups(self, keep: int = 5) -> None:
        """只保留最近 keep 个备份"""
        backups = self.list_backups()
        for old in backups[keep:]:
            old.unlink()
            logger.debug(f"清理旧备份: {old}")
