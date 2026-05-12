import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import Event
from app.core.event import eventmanager
from app.db.models.transferhistory import TransferHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.storage import StorageHelper
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.plugins import _PluginBase
from app.schemas import FileItem, TransferInfo
from app.schemas.types import EventType


class Fruits115(_PluginBase):
    plugin_name = "Fruits115"
    plugin_desc = "媒体整理完成后，将源文件复制/上传到指定存储驱动目录"
    plugin_icon = "directory.png"
    plugin_version = "1.0.4"
    plugin_author = "fruits"
    author_url = "https://github.com/yating1022"
    plugin_config_prefix = "fruits115_"
    plugin_order = 1
    auth_level = 1

    _enable: bool = False
    _onlyonce: bool = False
    _mp_media_prefix: str = ""
    _target_storage: str = ""
    _target_path: str = ""
    _transfer_type: str = "copy"

    _storagehelper = StorageHelper()
    _storage_oper_cache: Dict[str, StorageBase] = {}

    # ---------------------------------------------------------------------------
    # 生命周期
    # ---------------------------------------------------------------------------

    def init_plugin(self, config: dict = None):
        if config:
            self._enable = config.get("enable") or False
            self._onlyonce = config.get("onlyonce") or False
            self._mp_media_prefix = (config.get("mp_media_prefix") or "").strip()
            self._target_storage = (config.get("target_storage") or "").strip()
            self._target_path = (config.get("target_path") or "").strip()
            self._transfer_type = (config.get("transfer_type") or "copy").strip()

        if self._onlyonce:
            # 立即运行一次
            self._run_once()
            # 关闭开关并保存
            self._onlyonce = False
            self.__update_config()

    def __update_config(self):
        self.update_config({
            "enable": self._enable,
            "onlyonce": self._onlyonce,
            "mp_media_prefix": self._mp_media_prefix,
            "target_storage": self._target_storage,
            "target_path": self._target_path,
            "transfer_type": self._transfer_type,
        })

    def _run_once(self):
        """立即运行一次：取最近一条成功整理记录，模拟触发插件执行"""
        logger.info("立即运行一次：开始执行")

        if not self._target_storage or not self._target_path:
            logger.error("立即运行一次：目标存储驱动或目标路径未配置")
            return
        if not self._mp_media_prefix:
            logger.error("立即运行一次：MP媒体库前缀 未配置")
            return

        # 取最近一条成功记录
        try:
            records = TransferHistory.list_by_page(page=1, count=1, status=True)
            if not records:
                logger.error("立即运行一次：未找到成功的整理记录")
                return
            record = records[0]
        except Exception as e:
            logger.error(f"立即运行一次：查询整理记录失败：{e}")
            return

        logger.info(f"立即运行一次：选中记录 [{record.id}] {record.title} | {record.dest}")

        # 获取目标存储操作对象
        target_oper = self._get_target_storage_oper()
        if not target_oper:
            logger.error(f"立即运行一次：无法加载存储驱动 {self._target_storage}")
            return

        # 检查存储可用性
        try:
            if not target_oper.check():
                logger.error(f"立即运行一次：存储驱动 {self._target_storage} 连接失败")
                return
        except Exception as e:
            logger.error(f"立即运行一次：存储驱动连接异常：{e}")
            return

        # 提取媒体元数据
        media_meta = {
            "title": record.title,
            "type_value": record.type,
            "category": record.category,
            "year": record.year,
            "tmdbid": record.tmdbid,
            "season": record.seasons,
            "episode": record.episodes,
            "downloader": record.downloader,
            "download_hash": record.download_hash,
        }

        # 对记录中的每个文件执行插件逻辑
        source_files = record.files or []
        dest_path = record.dest or ""

        if not source_files:
            logger.warning("立即运行一次：记录无源文件清单")
            return

        for src in source_files:
            if not src:
                continue
            self._process_file(src, dest_path, target_oper, media_meta)

        logger.info("立即运行一次：执行完成")

    def stop_service(self):
        pass

    # ---------------------------------------------------------------------------
    # 存储驱动
    # ---------------------------------------------------------------------------

    def _get_storagies(self) -> List[Dict[str, str]]:
        """返回主项目已配置的存储驱动列表 [{type, name}]"""
        result = []
        for s in self._storagehelper.get_storagies():
            result.append({"type": s.type, "name": s.name or s.type})
        return result

    def _get_storage_oper(self, storage_type: str) -> Optional[StorageBase]:
        """按类型获取存储操作对象（单例缓存）"""
        if storage_type in self._storage_oper_cache:
            return self._storage_oper_cache[storage_type]
        try:
            module_map = {
                "local": ("app.modules.filemanager.storages.local", "LocalStorage"),
                "alipan": ("app.modules.filemanager.storages.alipan", "AlipanPan"),
                "u115": ("app.modules.filemanager.storages.u115", "U115Pan"),
                "rclone": ("app.modules.filemanager.storages.rclone", "RcloneStorage"),
                "alist": ("app.modules.filemanager.storages.alist", "AlistStorage"),
                "smb": ("app.modules.filemanager.storages.smb", "SmbStorage"),
            }
            if storage_type not in module_map:
                logger.error(f"不支持的存储类型：{storage_type}")
                return None
            module_path, class_name = module_map[storage_type]
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            oper = cls()
            self._storage_oper_cache[storage_type] = oper
            return oper
        except Exception as e:
            logger.error(f"加载存储驱动 {storage_type} 失败：{e}")
            return None

    def _get_target_storage_oper(self) -> Optional[StorageBase]:
        return self._get_storage_oper(self._target_storage) if self._target_storage else None

    def _supported_transtypes(self, storage_type: str) -> dict:
        """获取指定存储驱动支持的整理方式"""
        oper = self._get_storage_oper(storage_type)
        if not oper:
            return {}
        return oper.support_transtype()

    # ---------------------------------------------------------------------------
    # 事件监听
    # ---------------------------------------------------------------------------

    @eventmanager.register(EventType.TransferComplete)
    def transfer_complete(self, event: Event):
        if not self._enable:
            return

        transfer_info: Optional[TransferInfo] = event.event_data.get("transferinfo")
        if not transfer_info:
            return

        source_files = transfer_info.file_list or []
        target_files = transfer_info.file_list_new or []
        if not source_files or not target_files:
            logger.debug("转移事件缺少源或目标文件列表，跳过")
            return

        if not self._target_storage or not self._target_path:
            logger.warning("目标存储驱动或目标路径未配置，跳过")
            return

        target_oper = self._get_target_storage_oper()
        if not target_oper:
            logger.error(f"无法加载目标存储驱动：{self._target_storage}")
            return

        if len(source_files) != len(target_files):
            logger.warning(
                f"源/目标文件数量不一致，"
                f"source={len(source_files)} target={len(target_files)}，仅处理可配对部分"
            )

        # 从事件中提取媒体元数据，用于写入整理记录
        mediainfo = event.event_data.get("mediainfo")
        meta = event.event_data.get("meta")
        media_meta = {
            "title": getattr(mediainfo, "title", None) if mediainfo else None,
            "type": getattr(mediainfo, "type", None),
            "type_value": getattr(getattr(mediainfo, "type", None), "value", None) if mediainfo else None,
            "year": getattr(mediainfo, "year", None) if mediainfo else None,
            "tmdbid": getattr(mediainfo, "tmdb_id", None) if mediainfo else None,
            "category": getattr(mediainfo, "category", None) if mediainfo else None,
            "season": getattr(meta, "season", None) if meta else None,
            "episode": getattr(meta, "episode", None) if meta else None,
            "downloader": event.event_data.get("downloader"),
            "download_hash": event.event_data.get("download_hash"),
        }

        for source_file, target_file in zip(source_files, target_files):
            if not source_file or not target_file:
                continue
            self._process_file(source_file, target_file, target_oper, media_meta)

    def _process_file(self, source_path: str, dest_path: str, target_oper: StorageBase, media_meta: dict = None):
        if not self._mp_media_prefix:
            logger.warning("MP媒体库前缀 未配置，跳过")
            return

        if not dest_path.startswith(self._mp_media_prefix):
            logger.debug(f"目标路径不以 MP媒体库 前缀开头，跳过 dest={dest_path}")
            return

        relative_path = dest_path[len(self._mp_media_prefix):].lstrip("/\\")
        target_dir = Path(self._target_path) / Path(relative_path).parent
        new_name = Path(relative_path).name
        target_full = str(target_dir / new_name)

        logger.info(
            f"处理文件：{source_path} -> "
            f"{self._target_storage}:{target_full}（{self._transfer_type}）"
        )

        source_file = Path(source_path)
        if not source_file.exists():
            logger.error(f"源文件不存在：{source_path}")
            self._record_history(
                source_path=source_path,
                target_path=target_full,
                success=False,
                errmsg=f"源文件不存在：{source_path}",
                media_meta=media_meta,
            )
            return

        result = self._do_transfer(source_file, target_dir, new_name, target_oper)
        if result:
            logger.info(f"成功：{source_path} -> {self._target_storage}:{target_full}")
            self._record_history(
                source_path=source_path,
                target_path=target_full,
                success=True,
                media_meta=media_meta,
            )
        else:
            logger.error(f"失败：{source_path} -> {self._target_storage}:{target_full}")
            self._record_history(
                source_path=source_path,
                target_path=target_full,
                success=False,
                errmsg="文件传输失败",
                media_meta=media_meta,
            )

    def _do_transfer(
        self,
        source_file: Path,
        target_dir: Path,
        new_name: str,
        target_oper: StorageBase,
    ) -> bool:
        """
        执行文件整理，支持跨存储传输。
        source_file 始终为本地文件。
        - 本地存储：根据 transfer_type 调用 copy/move/link/softlink
        - 云存储：link/softlink 不支持，回退为 copy；move = upload 后删除源文件
        """
        transfer_type = self._transfer_type

        try:
            if self._target_storage == "local":
                # 本地 -> 本地：直接调用对应整理方法
                target_dir.mkdir(parents=True, exist_ok=True)
                source_item = FileItem(
                    storage="local",
                    path=str(source_file),
                    type="file",
                    name=source_file.name,
                    size=source_file.stat().st_size,
                )
                return self._do_local_transfer(source_item, target_dir, new_name, target_oper, transfer_type)

            # 本地 -> 云存储：link/softlink 不可用，回退为 copy
            if transfer_type in ("link", "softlink"):
                logger.info(f"云存储不支持 {transfer_type}，回退为 copy")
                transfer_type = "copy"

            folder_item = target_oper.get_folder(target_dir)
            if not folder_item:
                logger.error(f"无法创建或获取目标目录：{target_dir}")
                return False

            uploaded = target_oper.upload(
                target_dir=folder_item,
                local_path=source_file,
                new_name=new_name,
            )
            if not uploaded:
                return False

            # move 模式：上传成功后删除源文件
            if transfer_type == "move":
                try:
                    source_file.unlink()
                    logger.info(f"已删除源文件（move 模式）：{source_file}")
                except Exception as e:
                    logger.warning(f"删除源文件失败：{source_file}，{e}")

            return True
        except Exception as e:
            logger.error(f"文件传输异常：{e}")
            return False

    @staticmethod
    def _do_local_transfer(
        source_item: FileItem,
        target_dir: Path,
        new_name: str,
        target_oper: StorageBase,
        transfer_type: str,
    ) -> bool:
        """本地到本地整理，根据 transfer_type 调用对应方法"""
        try:
            if transfer_type == "copy":
                return target_oper.copy(source_item, target_dir, new_name)
            elif transfer_type == "move":
                return target_oper.move(source_item, target_dir, new_name)
            elif transfer_type == "link":
                target_file = target_dir / new_name
                return target_oper.link(source_item, target_file)
            elif transfer_type == "softlink":
                target_file = target_dir / new_name
                return target_oper.softlink(source_item, target_file)
            else:
                logger.error(f"不支持的整理方式：{transfer_type}")
                return False
        except Exception as e:
            logger.error(f"本地整理异常：{e}")
            return False

    def _record_history(
        self,
        source_path: str,
        target_path: str,
        success: bool,
        errmsg: str = None,
        media_meta: dict = None,
    ):
        """写入整理记录，与主项目 TransferHistoryOper 格式一致"""
        meta = media_meta or {}
        try:
            from app.db import DbOper
            from app.db.models.transferhistory import TransferHistory as TH
            import time
            TH(
                src=source_path,
                src_storage="local",
                dest=target_path,
                dest_storage=self._target_storage,
                mode=self._transfer_type,
                title=meta.get("title") or "Fruits115",
                type=meta.get("type_value"),
                category=meta.get("category"),
                year=meta.get("year"),
                tmdbid=meta.get("tmdbid"),
                seasons=meta.get("season"),
                episodes=meta.get("episode"),
                downloader=meta.get("downloader"),
                download_hash=meta.get("download_hash"),
                status=success,
                errmsg=errmsg,
                date=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                files=[source_path],
            ).create(DbOper()._db)
        except Exception as e:
            logger.error(f"写入整理记录失败：{e}")

    # ---------------------------------------------------------------------------
    # 插件状态 & API
    # ---------------------------------------------------------------------------

    def get_state(self) -> bool:
        return self._enable

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/history",
                "endpoint": self.get_history,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "获取最近整理记录",
                "description": "返回最近 20 条成功的转移记录",
            },
        ]

    async def get_history(self):
        """
        GET /api/v1/plugin/Fruits115/history
        返回最近的成功转移记录供测试选择
        """
        try:
            records = TransferHistory.list_by_page(page=1, count=20, status=True)
            if not records:
                return {"success": True, "data": []}
            items = []
            for r in records:
                label = f"{r.title or '未知'} | {r.dest or r.src}"
                items.append({
                    "title": label,
                    "value": r.id,
                    "src": r.src,
                    "dest": r.dest,
                    "files": r.files or [],
                })
            return {"success": True, "data": items}
        except Exception as e:
            logger.error(f"获取整理记录失败：{e}")
            return {"success": False, "message": str(e)}


    # ---------------------------------------------------------------------------
    # 配置页面
    # ---------------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        storagies = self._get_storagies()
        storage_items = [{"title": s["name"], "value": s["type"]} for s in storagies]

        return [
            {
                "component": "VForm",
                "content": [
                    # 启用开关 + 立即运行一次
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enable",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # MP媒体库前缀
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "mp_media_prefix",
                                            "label": "MP媒体库前缀",
                                            "placeholder": "/media/library",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # 目标存储 + 目标路径
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "target_storage",
                                            "label": "目标存储驱动",
                                            "items": storage_items,
                                            "placeholder": "请选择存储驱动",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "target_path",
                                            "label": "目标路径",
                                            "placeholder": "/115/fruits/media",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 整理方式
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VRadioGroup",
                                        "props": {
                                            "model": "transfer_type",
                                            "label": "整理方式",
                                            "inline": True,
                                        },
                                        "content": [
                                            {
                                                "component": "VRadio",
                                                "props": {
                                                    "label": "复制 (copy)",
                                                    "value": "copy",
                                                },
                                            },
                                            {
                                                "component": "VRadio",
                                                "props": {
                                                    "label": "移动 (move)",
                                                    "value": "move",
                                                },
                                            },
                                            {
                                                "component": "VRadio",
                                                "props": {
                                                    "label": "硬链接 (link)",
                                                    "value": "link",
                                                },
                                            },
                                            {
                                                "component": "VRadio",
                                                "props": {
                                                    "label": "软链接 (softlink)",
                                                    "value": "softlink",
                                                },
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    # 说明
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "监听转移完成事件：当 transfer.dest 以 MP媒体库前缀 开头时，将 transfer.src 文件整理到指定存储驱动的目标路径下。整理方式中，link/softlink 仅对本地存储有效，云存储将自动回退为 copy。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "success",
                                            "variant": "tonal",
                                            "text": "逻辑示意：TransferComplete -> 读取 file_list/file_list_new -> 判断 dest.startswith(mp_media_prefix) -> 计算相对路径 -> 上传/复制到目标存储驱动指定目录",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enable": self._enable,
            "onlyonce": self._onlyonce,
            "mp_media_prefix": self._mp_media_prefix,
            "target_storage": self._target_storage,
            "target_path": self._target_path,
            "transfer_type": self._transfer_type,
        }

    def get_page(self) -> Optional[List[dict]]:
        pass
