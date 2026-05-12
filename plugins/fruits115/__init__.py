import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request

from app.core.event import Event
from app.core.event import eventmanager
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
    plugin_version = "1.0.0"
    plugin_author = "fruits"
    author_url = "https://github.com/honue"
    plugin_config_prefix = "fruits115_"
    plugin_order = 1
    auth_level = 1

    _enable: bool = False
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
            self._mp_media_prefix = (config.get("mp_media_prefix") or "").strip()
            self._target_storage = (config.get("target_storage") or "").strip()
            self._target_path = (config.get("target_path") or "").strip()
            self._transfer_type = (config.get("transfer_type") or "copy").strip()

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
            logger.debug("[Fruits115] 转移事件缺少源或目标文件列表，跳过")
            return

        if not self._target_storage or not self._target_path:
            logger.warning("[Fruits115] 目标存储驱动或目标路径未配置，跳过")
            return

        target_oper = self._get_target_storage_oper()
        if not target_oper:
            logger.error(f"[Fruits115] 无法加载目标存储驱动：{self._target_storage}")
            return

        if len(source_files) != len(target_files):
            logger.warning(
                f"[Fruits115] 源/目标文件数量不一致，"
                f"source={len(source_files)} target={len(target_files)}，仅处理可配对部分"
            )

        for source_file, target_file in zip(source_files, target_files):
            if not source_file or not target_file:
                continue
            self._process_file(source_file, target_file, target_oper)

    def _process_file(self, source_path: str, dest_path: str, target_oper: StorageBase):
        if not self._mp_media_prefix:
            logger.warning("[Fruits115] MP媒体库前缀 未配置，跳过")
            return

        if not dest_path.startswith(self._mp_media_prefix):
            logger.debug(f"[Fruits115] 目标路径不以 MP媒体库 前缀开头，跳过 dest={dest_path}")
            return

        relative_path = dest_path[len(self._mp_media_prefix):].lstrip("/\\")
        target_dir = Path(self._target_path) / Path(relative_path).parent
        new_name = Path(relative_path).name

        logger.info(
            f"[Fruits115] 处理文件：{source_path} -> "
            f"{self._target_storage}:{target_dir / new_name}（{self._transfer_type}）"
        )

        source_file = Path(source_path)
        if not source_file.exists():
            logger.error(f"[Fruits115] 源文件不存在：{source_path}")
            return

        result = self._do_transfer(source_file, target_dir, new_name, target_oper)
        if result:
            logger.info(f"[Fruits115] 成功：{source_path} -> {self._target_storage}:{target_dir / new_name}")
        else:
            logger.error(f"[Fruits115] 失败：{source_path} -> {self._target_storage}:{target_dir / new_name}")

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
                logger.info(f"[Fruits115] 云存储不支持 {transfer_type}，回退为 copy")
                transfer_type = "copy"

            folder_item = target_oper.get_folder(target_dir)
            if not folder_item:
                logger.error(f"[Fruits115] 无法创建或获取目标目录：{target_dir}")
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
                    logger.info(f"[Fruits115] 已删除源文件（move 模式）：{source_file}")
                except Exception as e:
                    logger.warning(f"[Fruits115] 删除源文件失败：{source_file}，{e}")

            return True
        except Exception as e:
            logger.error(f"[Fruits115] 文件传输异常：{e}")
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
                logger.error(f"[Fruits115] 不支持的整理方式：{transfer_type}")
                return False
        except Exception as e:
            logger.error(f"[Fruits115] 本地整理异常：{e}")
            return False

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
                "path": "/test_storage",
                "endpoint": self.test_storage,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "测试存储驱动连接",
                "description": "测试指定存储驱动是否可用，验证目标路径是否存在",
            }
        ]

    async def test_storage(self, request: Request):
        """
        测试存储驱动连接
        POST /api/v1/plugin/Fruits115/test_storage
        Body: {"storage": "u115", "path": "/some/path"}
        """
        try:
            body = await request.json() if request.headers.get("content-type") == "application/json" else {}
        except Exception:
            body = {}
        storage = (body.get("storage") or "").strip() or self._target_storage
        path = (body.get("path") or "").strip() or self._target_path
        if not storage:
            return {"success": False, "message": "未指定存储驱动"}

        oper = self._get_storage_oper(storage)
        if not oper:
            return {"success": False, "message": f"无法加载存储驱动：{storage}"}

        try:
            available = oper.check()
            if not available:
                return {"success": False, "message": f"存储驱动 {storage} 连接失败"}
        except Exception as e:
            return {"success": False, "message": f"存储驱动 {storage} 连接异常：{e}"}

        if path:
            try:
                folder_item = oper.get_folder(Path(path))
                if not folder_item:
                    return {"success": False, "message": f"目标路径不可访问：{path}"}
            except Exception as e:
                return {"success": False, "message": f"目标路径访问异常：{e}"}

        return {"success": True, "message": f"存储驱动 {storage} 连接正常"}

    # ---------------------------------------------------------------------------
    # 配置页面
    # ---------------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 存储驱动选项
        storagies = self._get_storagies()
        storage_items = [{"title": s["name"], "value": s["type"]} for s in storagies]

        return [
            {
                "component": "VForm",
                "content": [
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
                                        "component": "VBtn",
                                        "props": {
                                            "class": "mt-2",
                                            "variant": "tonal",
                                            "color": "info",
                                            "onClick": {
                                                "action": "call",
                                                "url": "/plugin/Fruits115/test_storage",
                                                "method": "POST",
                                            },
                                        },
                                        "text": "测试连接",
                                    }
                                ],
                            },
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
            "mp_media_prefix": self._mp_media_prefix,
            "target_storage": self._target_storage,
            "target_path": self._target_path,
            "transfer_type": self._transfer_type,
        }

    def get_page(self) -> Optional[List[dict]]:
        pass
