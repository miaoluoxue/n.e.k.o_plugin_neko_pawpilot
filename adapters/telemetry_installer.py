"""遥测插件受控导入：用户确认后安装 scs-telemetry.dll。

启动时只检测状态，不写游戏目录。用户明确确认后才安装；覆盖已有不同版本前，
先把原文件备份到插件数据目录并校验 SHA-256。
目标/捆绑路径来自配置（telemetry_plugin_rel / telemetry_bundle_rel）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Dict, Optional

from .game_dir import detect_game_dir


class TelemetryInstaller:
    """检测并按用户确认安装/校验遥测插件文件。"""

    def __init__(self, plugin_rel: Optional[str] = None,
                 bundle_rel: Optional[str] = None,
                 backup_dir: Optional[Path] = None) -> None:
        from ..core.config_model import DEFAULTS
        self._plugin_rel = Path(plugin_rel or DEFAULTS["telemetry_plugin_rel"])
        self._bundled = (Path(__file__).resolve().parent.parent
                         / (bundle_rel or DEFAULTS["telemetry_bundle_rel"]))
        self._backup_dir = Path(backup_dir) if backup_dir else None

    def bundled_available(self) -> bool:
        return self._bundled.exists()

    def bundled_hash(self) -> str:
        return self._sha256(self._bundled) if self._bundled.exists() else ""

    def detect_game_dir(self) -> Optional[str]:
        return detect_game_dir()

    def target_path(self, game_dir: str) -> Path:
        return Path(game_dir, self._plugin_rel)

    def installed_ok(self, game_dir: str) -> bool:
        """已安装且与捆绑版本一致。"""
        target = self.target_path(game_dir)
        return (target.exists()
                and self._sha256(target) == self.bundled_hash())

    def status(self, game_dir: Optional[str] = None) -> Dict[str, str]:
        """只读检查安装状态；不创建目录、不复制、不覆盖文件。"""
        if not self.bundled_available():
            return {"status": "no_bundle", "detail": "插件缺少捆绑遥测文件"}
        game_dir = game_dir or self.detect_game_dir()
        if not game_dir:
            return {"status": "no_game", "detail": "未找到欧卡2安装目录"}
        target = self.target_path(game_dir)
        if not target.exists():
            return {
                "status": "install_required",
                "detail": "遥测文件尚未安装，需要用户确认",
                "target": str(target),
            }
        if self._sha256(target) == self.bundled_hash():
            return {
                "status": "ok",
                "detail": "遥测插件已就绪",
                "target": str(target),
            }
        return {
            "status": "update_required",
            "detail": "检测到不同版本；确认后会先备份原文件再更新",
            "target": str(target),
        }

    def install(self, game_dir: Optional[str] = None,
                confirm: bool = False) -> Dict[str, str]:
        """用户确认后导入遥测 DLL，覆盖前先备份并做原子替换。

        返回 {status, detail}：
        - confirm_required: 尚未得到用户确认，未写任何文件
        - install_required: 只读检查结果，待确认安装
        - update_required: 只读检查结果，待确认更新
        - ok: 已就绪（版本一致，无需操作）
        - installed: 本次完成导入
        - updated: 备份原文件后覆盖为捆绑版本
        - backup_failed: 原文件备份/校验失败，未覆盖
        - locked: 游戏运行中无法写入，需重启游戏
        - no_game: 未找到游戏目录
        - no_bundle: 插件缺少捆绑 DLL
        """
        state = self.status(game_dir)
        if not confirm:
            return {
                "status": "confirm_required",
                "detail": "安装或更新遥测 DLL 需要用户明确确认",
                "target": state.get("target", ""),
            }
        if state["status"] not in ("install_required", "update_required"):
            return state
        game_dir = game_dir or self.detect_game_dir()
        target = self.target_path(game_dir)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return {"status": "locked", "detail": "无法创建游戏插件目录，请检查权限"}

        backup_path = ""
        if state["status"] == "update_required":
            try:
                backup_path = str(self._backup(target))
            except OSError as exc:
                return {
                    "status": "backup_failed",
                    "detail": f"原文件备份或校验失败，已停止覆盖：{exc}",
                    "target": str(target),
                }
        try:
            self._atomic_copy(self._bundled, target)
        except OSError as exc:
            return {
                "status": "locked",
                "detail": f"遥测文件被占用或写入失败，请关闭游戏后重试：{exc}",
                "backup": backup_path,
                "target": str(target),
            }
        if backup_path:
            return {
                "status": "updated",
                "detail": "原遥测文件已备份，已更新为插件捆绑版本",
                "backup": backup_path,
                "target": str(target),
            }
        return {
            "status": "installed",
            "detail": "遥测插件安装完成，重启游戏生效",
            "target": str(target),
        }

    def _backup(self, target: Path) -> Path:
        """复制并校验原 DLL；备份目录优先使用插件私有数据目录。"""
        backup_dir = self._backup_dir or target.parent
        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = backup_dir / f"{target.name}.{time.time_ns()}.bak"
        original_hash = self._sha256(target)
        if not original_hash:
            raise OSError("无法读取原遥测文件")
        shutil.copy2(target, destination)
        if self._sha256(destination) != original_hash:
            destination.unlink(missing_ok=True)
            raise OSError("备份文件 SHA-256 与原文件不一致")
        return destination

    def _atomic_copy(self, source: Path, target: Path) -> None:
        """先写同目录临时文件并校验，再原子替换目标，避免半截 DLL。"""
        expected_hash = self._sha256(source)
        if not expected_hash:
            raise OSError("无法读取捆绑遥测文件")
        temporary = target.with_name(
            f".{target.name}.{time.time_ns()}.neko-tmp")
        try:
            shutil.copy2(source, temporary)
            if self._sha256(temporary) != expected_hash:
                raise OSError("临时文件 SHA-256 校验失败")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""
