#!/usr/bin/env python3

import asyncio
import pickle
import aiohttp
import aiofiles
from pathlib import Path
from typing import List, Dict, Any, Optional, Literal, Tuple
import logging
import sys
import os
from dataclasses import dataclass
from contextlib import asynccontextmanager
from datetime import datetime

from qqmusic_api import user, songlist, song
from qqmusic_api.song import get_song_urls, SongFileType
from qqmusic_api.login import Credential, check_expired
from qqmusic_api.lyric import get_lyric
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, USLT


## 配置常量
class Config:
    BATCH_SIZE = 5
    COVER_SIZE = 800
    DOWNLOAD_TIMEOUT = 30
    CREDENTIAL_FILE = Path("qqmusic_cred.pkl")
    MUSIC_DIR = Path("./music")
    FOLDER_NAME = "{songlist_name}"  # 歌单文件夹名称格式
    # FOLDER_NAME = "用户{user_id}_{songlist_name}"
    MIN_FILE_SIZE = 1024  # 最小文件大小检查
    EXTERNAL_API_URL = "https://api.ygking.top"  # 外部API地址


## 日志配置 - 只显示警告和错误
def setup_logging():
    """配置日志系统"""
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    # 特别设置qqmusic_api的日志级别为WARNING，隐藏HTTP请求日志
    logging.getLogger("qqmusic_api").setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger(__name__)


@dataclass
class SongInfo:
    """歌曲信息数据类"""
    name: str
    singer: str
    mid: str
    is_vip: bool
    album_name: str
    album_mid: str


class DownloadError(Exception):
    """下载错误异常"""
    pass


class MetadataError(Exception):
    """元数据处理错误异常"""
    pass


class NetworkManager:
    """网络请求管理器"""

    def __init__(self):
        self.session = None

    @asynccontextmanager
    async def get_session(self):
        """获取会话的上下文管理器"""
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=Config.DOWNLOAD_TIMEOUT)
            self.session = aiohttp.ClientSession(timeout=timeout)

        try:
            yield self.session
        except Exception as e:
            raise DownloadError(f"网络请求失败: {e}")

    async def close(self):
        """关闭会话"""
        if self.session:
            await self.session.close()
            self.session = None


class FileManager:
    """文件管理类"""

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """清理文件名中的非法字符"""
        illegal_chars = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
        for char in illegal_chars:
            filename = filename.replace(char, '_')
        return filename.strip()

    @staticmethod
    def ensure_directory(path: Path) -> Path:
        """确保目录存在"""
        path.mkdir(parents=True, exist_ok=True)
        return path


class CoverManager:
    """封面管理类"""

    @staticmethod
    def get_cover_url_by_album_mid(mid: str, size: Literal[150, 300, 500, 800] = 800) -> Optional[str]:
        """通过专辑MID获取封面URL"""
        if not mid:
            return None
        if size not in [150, 300, 500, 800]:
            raise ValueError("不支持的封面尺寸")
        return f"https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{mid}.jpg"

    @staticmethod
    def get_cover_url_by_vs(vs: str, size: Literal[150, 300, 500, 800] = 800) -> Optional[str]:
        """通过VS值获取封面URL"""
        if not vs:
            return None
        if size not in [150, 300, 500, 800]:
            raise ValueError("不支持的封面尺寸")
        return f"https://y.qq.com/music/photo_new/T062R{size}x{size}M000{vs}.jpg"

    @staticmethod
    async def get_valid_cover_url(song_data: Dict[str, Any], network: NetworkManager,
                                  size: Literal[150, 300, 500, 800] = 800) -> Optional[str]:
        """获取并验证有效的封面URL（按优先级尝试所有可能的VS值）"""
        # 1. 优先尝试专辑MID
        album_mid = song_data.get('album', {}).get('mid', '')
        if album_mid:
            url = CoverManager.get_cover_url_by_album_mid(album_mid, size)
            logger.debug(f"尝试专辑MID封面: {url}")
            cover_data = await CoverManager.download_cover(url, network)
            if cover_data:
                logger.info(f"使用专辑MID封面: {url}")
                return url

        # 2. 尝试所有可用的VS值（按顺序）
        vs_values = song_data.get('vs', [])
        logger.debug(f"分析VS值: {vs_values}")

        # 收集所有候选VS值
        candidate_vs = []

        # 首先收集所有单个有效的VS值
        for i, vs in enumerate(vs_values):
            if vs and isinstance(vs, str) and len(vs) >= 3 and ',' not in vs:
                candidate_vs.append({
                    'value': vs,
                    'source': f'vs_single_{i}',
                    'priority': 1  # 高优先级
                })

        # 然后收集逗号分隔的VS值部分
        for i, vs in enumerate(vs_values):
            if vs and ',' in vs:
                parts = [part.strip() for part in vs.split(',') if part.strip()]
                for j, part in enumerate(parts):
                    if len(part) >= 3:
                        candidate_vs.append({
                            'value': part,
                            'source': f'vs_part_{i}_{j}',
                            'priority': 2  # 中优先级
                        })

        # 按优先级排序
        candidate_vs.sort(key=lambda x: x['priority'])

        logger.debug(f"候选VS值: {[c['value'] for c in candidate_vs]}")

        # 按顺序尝试每个候选VS值
        for candidate in candidate_vs:
            url = CoverManager.get_cover_url_by_vs(candidate['value'], size)
            logger.debug(f"尝试VS值封面 [{candidate['source']}]: {url}")
            cover_data = await CoverManager.download_cover(url, network)
            if cover_data:
                logger.info(f"使用VS值封面 [{candidate['source']}]: {url}")
                return url


        logger.warning("未找到任何有效的封面URL")
        return None

    @staticmethod
    async def download_cover(url: str, network: NetworkManager) -> Optional[bytes]:
        """下载封面图片"""
        if not url:
            return None

        try:
            async with network.get_session() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        # 检查文件大小和内容有效性
                        if len(content) > Config.MIN_FILE_SIZE:
                            # 简单验证图片格式
                            if content.startswith(b'\xff\xd8') or content.startswith(b'\x89PNG'):
                                logger.debug(f"封面下载成功: {len(content)} bytes")
                                return content
                            else:
                                logger.warning(f"封面图片格式无效: {url}")
                        else:
                            logger.warning(f"封面图片过小: {len(content)} bytes, URL: {url}")
                    else:
                        return None
                    return None
        except Exception as e:
            logger.error(f"封面下载异常: {e}, URL: {url}")
            return None


class MetadataManager:
    """元数据管理类"""

    def __init__(self, network: NetworkManager):
        self.network = network

    async def add_metadata_to_flac(self, file_path: Path, song_info: SongInfo,
                                   lyrics_data: dict = None, song_data: Dict[str, Any] = None) -> bool:
        """为FLAC文件添加元数据"""
        try:
            audio = FLAC(file_path)

            # 设置基本元数据
            self._set_basic_metadata(audio, song_info)

            # 添加封面
            if song_data:
                await self._add_cover_to_flac(audio, song_data)

            # 添加歌词
            if lyrics_data:
                self._add_lyrics_to_flac(audio, lyrics_data)

            audio.save()
            return True

        except Exception as e:
            logger.error(f"FLAC元数据添加失败: {e}")
            raise MetadataError(f"FLAC元数据处理失败: {e}")

    async def add_metadata_to_mp3(self, file_path: Path, song_info: SongInfo,
                                  lyrics_data: dict = None, song_data: Dict[str, Any] = None) -> bool:
        """为MP3文件添加元数据"""
        try:
            # 确保文件存在且可读
            if not file_path.exists():
                logger.error(f"文件不存在: {file_path}")
                return False

            # 尝试读取现有ID3标签，如果不存在则创建新的
            try:
                audio = ID3(file_path)
            except Exception:
                audio = ID3()

            # 清除现有的封面和歌词标签
            self._clear_existing_mp3_tags(audio)

            # 设置基本元数据
            self._set_basic_metadata_mp3(audio, song_info)

            # 添加封面
            if song_data:
                await self._add_cover_to_mp3(audio, song_data)

            # 添加歌词
            if lyrics_data:
                self._add_lyrics_to_mp3(audio, lyrics_data)

            # 保存标签
            audio.save(file_path, v2_version=3)  # 使用ID3v2.3确保兼容性
            logger.debug(f"MP3元数据添加成功: {file_path}")
            return True

        except Exception as e:
            logger.error(f"MP3元数据添加失败: {e}")
            raise MetadataError(f"MP3元数据处理失败: {e}")

    def _clear_existing_mp3_tags(self, audio):
        """清除现有的MP3标签"""
        tags_to_remove = ['APIC:', 'USLT:', 'TIT2', 'TPE1', 'TALB']
        for tag in tags_to_remove:
            if tag in audio:
                del audio[tag]

    def _set_basic_metadata(self, audio, song_info: SongInfo):
        """设置基本元数据(FLAC)"""
        audio['title'] = song_info.name
        audio['artist'] = song_info.singer
        audio['album'] = song_info.album_name

    def _set_basic_metadata_mp3(self, audio, song_info: SongInfo):
        """设置基本元数据(MP3)"""
        try:
            # 使用UTF-8编码确保中文正确显示
            audio.add(TIT2(encoding=3, text=song_info.name))
            audio.add(TPE1(encoding=3, text=song_info.singer))
            audio.add(TALB(encoding=3, text=song_info.album_name))
        except Exception as e:
            logger.error(f"设置MP3基本元数据失败: {e}")

    async def _add_cover_to_flac(self, audio, song_data: Dict[str, Any]):
        """为FLAC添加封面"""
        cover_url = await CoverManager.get_valid_cover_url(song_data, self.network, Config.COVER_SIZE)
        if cover_url:
            cover_data = await CoverManager.download_cover(cover_url, self.network)
            if cover_data:
                image = Picture()
                image.type = 3
                # 根据URL判断图片类型
                if cover_url.lower().endswith('.png'):
                    image.mime = 'image/png'
                else:
                    image.mime = 'image/jpeg'
                image.desc = 'Cover'
                image.data = cover_data

                audio.clear_pictures()
                audio.add_picture(image)
                logger.info("FLAC封面添加成功")

    async def _add_cover_to_mp3(self, audio, song_data: Dict[str, Any]):
        """为MP3添加封面"""
        try:
            cover_url = await CoverManager.get_valid_cover_url(song_data, self.network, Config.COVER_SIZE)
            if cover_url:
                cover_data = await CoverManager.download_cover(cover_url, self.network)
                if cover_data:
                    # 检测图片类型
                    if cover_url.lower().endswith('.png'):
                        mime_type = 'image/png'
                    else:
                        mime_type = 'image/jpeg'

                    # 添加封面图片
                    audio.add(APIC(
                        encoding=3,  # UTF-8
                        mime=mime_type,
                        type=3,  # 封面图片
                        desc='Cover',
                        data=cover_data
                    ))
                    logger.info("MP3封面添加成功")
        except Exception as e:
            logger.error(f"添加MP3封面失败: {e}")

    def _add_lyrics_to_flac(self, audio, lyrics_data: dict):
        """为FLAC添加歌词"""
        if lyric_text := lyrics_data.get('lyric'):
            audio['lyrics'] = lyric_text
        if trans_text := lyrics_data.get('trans'):
            audio['translyrics'] = trans_text

    def _add_lyrics_to_mp3(self, audio, lyrics_data: dict):
        """为MP3添加歌词"""
        try:
            if lyric_text := lyrics_data.get('lyric'):
                audio.add(USLT(
                    encoding=3,
                    lang='eng',
                    desc='Lyrics',
                    text=lyric_text
                ))
                logger.debug("MP3歌词添加成功")
        except Exception as e:
            logger.error(f"添加MP3歌词失败: {e}")


class CredentialManager:
    """凭证管理器"""

    def __init__(self, credential_file: Path = Config.CREDENTIAL_FILE,
                 external_api_url: str = Config.EXTERNAL_API_URL):
        self.credential_file = credential_file
        self.external_api_url = external_api_url.rstrip('/') if external_api_url else ""
        self.credential_loaded = False
        self.credential_refreshed = False
        self.loaded_from_api = False

    async def load_and_refresh_credential(self) -> Optional[Credential]:
        """加载并刷新凭证"""
        self.credential_loaded = False
        self.credential_refreshed = False
        self.loaded_from_api = False

        # 优先尝试从本地文件加载
        if self.credential_file.exists():
            try:
                with self.credential_file.open("rb") as f:
                    cred: Credential = pickle.load(f)

                if await check_expired(cred):
                    refreshed_cred = await self._refresh_credential(cred)
                    if refreshed_cred:
                        self.credential_loaded = True
                        self.credential_refreshed = True
                        return refreshed_cred
                    else:
                        # 本地凭证过期且无法刷新，尝试从外部API加载
                        return await self._try_load_from_api()

                self.credential_loaded = True
                return cred

            except Exception as e:
                # 本地文件加载失败，尝试从外部API加载
                return await self._try_load_from_api()
        
        # 本地文件不存在，尝试从外部API加载
        return await self._try_load_from_api()

    async def _try_load_from_api(self) -> Optional[Credential]:
        """尝试从外部API加载凭证"""
        if not self.external_api_url:
            return None
        
        try:
            cred = await self.load_from_external_api()
            if cred:
                self.credential_loaded = True
                self.loaded_from_api = True
                return cred
        except Exception:
            pass
        
        return None

    async def load_from_external_api(self) -> Optional[Credential]:
        """从外部API加载凭证"""
        if not self.external_api_url:
            return None
        
        url = f"{self.external_api_url}/api/credential"
        
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        cred_data = data.get('credential', {})
                        
                        if not cred_data or not cred_data.get('musicid') or not cred_data.get('musickey'):
                            return None
                        
                        # 构建Credential对象
                        cred = Credential(
                            openid=cred_data.get('openid', ''),
                            refresh_token=cred_data.get('refresh_token', ''),
                            access_token=cred_data.get('access_token', ''),
                            expired_at=cred_data.get('expired_at', 0),
                            musicid=cred_data.get('musicid', 0),
                            musickey=cred_data.get('musickey', ''),
                            unionid=cred_data.get('unionid', ''),
                            str_musicid=cred_data.get('str_musicid', ''),
                            refresh_key=cred_data.get('refresh_key', ''),
                            encrypt_uin=cred_data.get('encrypt_uin', ''),
                            login_type=cred_data.get('login_type', 2)
                        )
                        
                        return cred
                    else:
                        return None
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

    async def _refresh_credential(self, cred: Credential) -> Optional[Credential]:
        """刷新凭证"""
        if await cred.can_refresh():
            try:
                await cred.refresh()
                with self.credential_file.open("wb") as f:
                    pickle.dump(cred, f)
                self.credential_refreshed = True
                return cred
            except Exception:
                return None
        else:
            return None

    def get_credential_info(self) -> str:
        """获取凭证文件信息"""
        if not self.credential_file.exists():
            return "凭证文件: 不存在"

        try:
            file_mtime = datetime.fromtimestamp(self.credential_file.stat().st_mtime)
            return f"修改时间: {file_mtime.strftime('%Y-%m-%d %H:%M:%S')}"
        except Exception as e:
            return f"修改时间: 无法获取 ({e})"


class DownloadLogger:
    """下载日志记录器"""

    def __init__(self):
        self.successful_downloads = []
        self.failed_downloads = []

    def log_success(self, song_info: SongInfo, quality: str, file_path: Path):
        """记录成功下载"""
        log_entry = {
            'song': f"{song_info.singer} - {song_info.name}",
            'quality': quality,
            'file_path': str(file_path),
            'timestamp': datetime.now().isoformat(),
            'vip': song_info.is_vip
        }
        self.successful_downloads.append(log_entry)

        # 使用print输出
        vip_mark = " [VIP]" if song_info.is_vip else ""
        message = f"下载成功: ---> {file_path.name}"
        print(f"  {message}")

    def log_failure(self, song_info: SongInfo, reason: str):
        """记录下载失败"""
        log_entry = {
            'song': f"{song_info.singer} - {song_info.name}",
            'reason': reason,
            'timestamp': datetime.now().isoformat(),
            'vip': song_info.is_vip
        }
        self.failed_downloads.append(log_entry)

        # 使用print输出
        vip_mark = " [VIP]" if song_info.is_vip else ""
        message = f"下载失败: {song_info.singer} - {song_info.name}{vip_mark} - {reason}"
        print(f"  {message}")

    def log_skip(self, song_info: SongInfo, file_path: Path):
        """记录跳过下载（文件已存在）"""
        message = f"文件已存在，跳过: {song_info.singer} - {song_info.name} -> {file_path.name}"
        print(f"  {message}")

    def get_summary(self) -> Dict[str, Any]:
        """获取下载摘要"""
        return {
            'total_successful': len(self.successful_downloads),
            'total_failed': len(self.failed_downloads),
            'successful_downloads': self.successful_downloads,
            'failed_downloads': self.failed_downloads,
            'timestamp': datetime.now().isoformat()
        }

    def print_summary(self):
        """打印下载摘要"""
        print("\n" + "=" * 60)
        print("下载摘要:")
        print(f"成功: {len(self.successful_downloads)} 首")
        print(f"失败: {len(self.failed_downloads)} 首")

        if self.successful_downloads:
            print("\n成功下载的歌曲:")
            for i, download in enumerate(self.successful_downloads, 1):
                vip_mark = " [VIP]" if download['vip'] else ""
                print(f"  {i}. {download['song']}{vip_mark} ({download['quality']})")

        if self.failed_downloads:
            print("\n下载失败的歌曲:")
            for i, download in enumerate(self.failed_downloads, 1):
                vip_mark = " [VIP]" if download['vip'] else ""
                print(f"  {i}. {download['song']}{vip_mark} - {download['reason']}")

        print("=" * 60)


class QQMusicDownloader:
    """QQ音乐下载器"""

    def __init__(self, download_dir: Path = Config.MUSIC_DIR):
        self.download_dir = FileManager.ensure_directory(download_dir)
        self.credential = None
        self.quality_level = 3  # 默认 FLAC 无损

        # 初始化组件
        self.network = NetworkManager()
        self.file_manager = FileManager()
        self.credential_manager = CredentialManager()
        self.metadata_manager = MetadataManager(self.network)
        self.download_logger = DownloadLogger()

    async def initialize(self):
        """初始化下载器"""
        await self.network.get_session().__aenter__()
        self.credential = await self.credential_manager.load_and_refresh_credential()

    async def close(self):
        """关闭下载器"""
        await self.network.close()

    def get_credential_info(self) -> Tuple[str, bool, bool, bool]:
        """获取凭证文件信息"""
        credential_info = self.credential_manager.get_credential_info()
        loaded = self.credential_manager.credential_loaded
        refreshed = self.credential_manager.credential_refreshed
        loaded_from_api = self.credential_manager.loaded_from_api
        return credential_info, loaded, refreshed, loaded_from_api

    def _check_credential(self) -> bool:
        """检查凭证是否有效"""
        if not self.credential:
            print("\n" + "=" * 50)
            print("请先运行登录程序获取凭证文件")
            print(f"凭证文件路径: {Config.CREDENTIAL_FILE.absolute()}")
            print("=" * 50)
            return False
        return True

    async def get_user_songlists(self, user_id: str) -> List[Dict[str, Any]]:
        """获取用户歌单列表"""
        if not self._check_credential():
            return []

        try:
            print(f"正在查询用户 {user_id} 的歌单...")
            songlists = await user.get_created_songlist(user_id, credential=self.credential)

            if not songlists:
                print("未找到该用户的歌单或歌单为空")
                return []

            return songlists

        except Exception as e:
            print(f"获取歌单失败: {e}")
            return []

    async def get_songlist_details(self, songlist_info: Dict[str, Any], user_id: str) -> List[Dict[str, Any]]:
        """获取歌单详情"""
        if not self._check_credential():
            return []

        try:
            dirid = songlist_info.get('dirId', 0)
            tid = songlist_info.get('tid', 0)

            # 处理"我喜欢"歌单的权限
            if dirid == 201 and self._is_other_user(user_id):
                print("权限不足!收藏歌单不公开!!")
                return []

            songs = await songlist.get_songlist(tid, dirid)
            print(f"歌单中有 {len(songs)} 首歌曲")
            return songs

        except Exception as e:
            print(f"获取歌单歌曲失败: {e}")
            return []

    def _is_other_user(self, user_id: str) -> bool:
        """检查是否为其他用户"""
        return (self.credential and hasattr(self.credential, 'musicid')
                and str(self.credential.musicid) != str(user_id))

    async def extract_song_info(self, song_data: Dict[str, Any]) -> SongInfo:
        """提取歌曲信息"""
        song_name = song_data.get('title', '未知歌曲')

        singer_info = song_data.get('singer', [])
        singer_name = (singer_info[0].get('name', '未知歌手')
                       if singer_info and isinstance(singer_info, list)
                       else '未知歌手')

        return SongInfo(
            name=song_name,
            singer=singer_name,
            mid=song_data.get('mid', ''),
            is_vip=song_data.get('pay', {}).get('pay_play', 0) != 0,
            album_name=song_data.get('album', {}).get('name', ''),
            album_mid=song_data.get('album', {}).get('mid', '')
        )

    # 音质选项: (显示名称, 降级链)
    QUALITY_OPTIONS = {
        1: ("臻品母带 (MASTER, 24Bit 192kHz)", [
            (SongFileType.MASTER, "臻品母带"),
            (SongFileType.ATMOS_2, "臻品全景声"),
            (SongFileType.ATMOS_51, "臻品音质"),
            (SongFileType.FLAC, "FLAC"),
            (SongFileType.MP3_320, "MP3 320kbps"),
            (SongFileType.MP3_128, "MP3 128kbps"),
        ]),
        2: ("臻品全景声 (ATMOS, 16Bit 44.1kHz)", [
            (SongFileType.ATMOS_2, "臻品全景声"),
            (SongFileType.ATMOS_51, "臻品音质"),
            (SongFileType.FLAC, "FLAC"),
            (SongFileType.MP3_320, "MP3 320kbps"),
            (SongFileType.MP3_128, "MP3 128kbps"),
        ]),
        3: ("FLAC 无损 (16Bit~24Bit)", [
            (SongFileType.FLAC, "FLAC"),
            (SongFileType.MP3_320, "MP3 320kbps"),
            (SongFileType.MP3_128, "MP3 128kbps"),
        ]),
        4: ("MP3 320kbps", [
            (SongFileType.MP3_320, "MP3 320kbps"),
            (SongFileType.MP3_128, "MP3 128kbps"),
        ]),
    }

    def _get_quality_strategy(self) -> List[Tuple[SongFileType, str]]:
        """获取音质下载策略"""
        _, fallback_chain = self.QUALITY_OPTIONS.get(self.quality_level, self.QUALITY_OPTIONS[3])
        return fallback_chain

    async def download_single_song(self, song_data: Dict[str, Any],
                                   folder: Path) -> bool:
        """下载单首歌曲"""
        if not self._check_credential():
            return False

        try:
            song_info = await self.extract_song_info(song_data)
            safe_filename = self.file_manager.sanitize_filename(
                f"{song_info.singer} - {song_info.name}"
            )

            # 尝试不同音质
            for file_type, quality_name in self._get_quality_strategy():
                file_path = folder / f"{safe_filename}{file_type.e}"

                if file_path.exists():
                    self.download_logger.log_skip(song_info, file_path)
                    return True

                success = await self._download_with_quality(
                    song_info, file_type, quality_name, file_path, safe_filename, song_data
                )
                if success:
                    return True

            self.download_logger.log_failure(song_info, "所有音质下载失败")
            return False

        except Exception as e:
            print(f"下载歌曲失败: {e}")
            self.download_logger.log_failure(
                await self.extract_song_info(song_data) if 'song_info' not in locals() else song_info,
                f"异常: {str(e)}"
            )
            return False

    async def _download_with_quality(self, song_info: SongInfo, file_type: SongFileType,
                                     quality_name: str, file_path: Path, safe_filename: str,
                                     song_data: Dict[str, Any]) -> bool:
        """使用指定音质下载"""
        print(f"尝试下载 {quality_name}: {safe_filename}{' [VIP]' if song_info.is_vip else ''}")

        urls = await get_song_urls([song_info.mid], file_type=file_type,
                                   credential=self.credential)
        url = urls.get(song_info.mid)

        if not url:
            print(f"无法获取歌曲URL ({quality_name}): {song_info.name}")
            return False

        async with self.network.get_session() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    content = await response.read()
                    if len(content) > Config.MIN_FILE_SIZE:
                        await self._save_file(file_path, content)
                        await self._add_metadata(file_path, song_info, song_data)
                        self.download_logger.log_success(song_info, quality_name, file_path)
                        return True
                    else:
                        print(f"文件过小，可能下载失败: {song_info.name}")
                else:
                    print(f"下载失败: {song_info.name}, 状态码: {response.status}")

        return False

    async def _save_file(self, file_path: Path, content: bytes):
        """保存文件"""
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(content)

    async def _add_metadata(self, file_path: Path, song_info: SongInfo, song_data: Dict[str, Any]):
        """添加元数据"""
        try:
            lyrics_data = await self._get_lyrics(song_info.mid)

            if file_path.suffix.lower() == '.flac':
                await self.metadata_manager.add_metadata_to_flac(
                    file_path, song_info, lyrics_data, song_data
                )
            elif file_path.suffix.lower() in ['.mp3', '.m4a']:
                await self.metadata_manager.add_metadata_to_mp3(
                    file_path, song_info, lyrics_data, song_data
                )

        except Exception as e:
            print(f"元数据添加失败 {song_info.name}: {e}")

    async def _get_lyrics(self, song_mid: str) -> Optional[dict]:
        """获取歌词"""
        try:
            return await get_lyric(song_mid)
        except Exception:
            return None

    async def preview_songlist(self, songlist_info: Dict[str, Any],
                               user_id: str) -> List[Dict[str, Any]]:
        """预览歌单"""
        print("正在获取歌单歌曲列表...")
        songs = await self.get_songlist_details(songlist_info, user_id)

        if not songs:
            print("无法获取歌单歌曲或歌单为空")
            return []

        songlist_name = songlist_info.get('dirName', '未知歌单')
        print(f"\n歌单 '{songlist_name}' 包含以下 {len(songs)} 首歌曲:")
        print("=" * 60)

        for i, song_data in enumerate(songs, 1):
            song_info = await self.extract_song_info(song_data)
            vip_mark = " [VIP]" if song_info.is_vip else ""
            print(f"{i:2d}. {song_info.singer} - {song_info.name}{vip_mark}")

        print("=" * 60)
        return songs

    async def download_songlist(self, songlist_info: Dict[str, Any],
                                user_id: str, songs: List[Dict[str, Any]]) -> Tuple[int, int]:
        """下载歌单"""
        if not self._check_credential():
            return 0, 0

        songlist_name = songlist_info.get('dirName', '未知歌单')
        safe_folder_name = self.file_manager.sanitize_filename(Config.FOLDER_NAME.format(user_id=user_id, songlist_name=songlist_name))
        folder = FileManager.ensure_directory(self.download_dir / safe_folder_name)

        quality_name, _ = self.QUALITY_OPTIONS.get(self.quality_level, self.QUALITY_OPTIONS[3])
        quality_chain = " -> ".join(name for _, name in self._get_quality_strategy())
        print(f"\n开始下载歌单: {songlist_name} (共 {len(songs)} 首歌曲)")
        print(f"下载音质策略: {quality_chain} (自动添加封面歌词)")
        print(f"保存位置: {folder}")
        print("-" * 60)

        success_count = 0
        failed_count = 0

        for i in range(0, len(songs), Config.BATCH_SIZE):
            batch = songs[i:i + Config.BATCH_SIZE]
            tasks = [self.download_single_song(song, folder) for song in batch]
            results = await asyncio.gather(*tasks)

            for success in results:
                if success:
                    success_count += 1
                else:
                    failed_count += 1

            total_done = min(i + Config.BATCH_SIZE, len(songs))
            progress = (total_done / len(songs)) * 100
            print(f"\n进度: {total_done}/{len(songs)} ({progress:.1f}%) - "
                  f"成功: {success_count}, 失败: {failed_count}")

            if i + Config.BATCH_SIZE < len(songs):
                await asyncio.sleep(1)

        # 显示下载摘要
        self.download_logger.print_summary()

        return success_count, failed_count


class InteractiveInterface:
    """交互式界面"""

    def __init__(self, downloader: QQMusicDownloader):
        self.downloader = downloader

    async def run(self):
        """运行交互界面"""
        print("QQ音乐歌单下载")
        print("版本号: v2.3.0")

        # 初始化下载器
        await self.downloader.initialize()

        # 获取凭证信息
        credential_info, loaded, refreshed, loaded_from_api = self.downloader.get_credential_info()

        # 显示凭证状态
        if loaded:
            if loaded_from_api:
                print(f"从外部API加载成功 ({Config.EXTERNAL_API_URL})")
            elif refreshed:
                print("使用本地凭证登录成功 (已自动刷新)")
            else:
                print("使用本地凭证登录成功")
        else:
            print("凭证加载失败")

        # 显示凭证文件信息
        if not loaded_from_api:
            print(credential_info)

        print("-" * 50)

        if not self.downloader.credential:
            self._show_credential_error()
            return

        while True:
            try:
                user_id = input("请输入你的musicid (输入'q'退出): ").strip()

                if user_id.lower() == 'q':
                    print("再见!")
                    break

                if not user_id:
                    print("musicid不能为空!")
                    continue

                await self._handle_user_session(user_id)

            except KeyboardInterrupt:
                print("\n再见!")
                break
            except Exception as e:
                print(f"交互界面错误: {e}")

    def _show_credential_error(self):
        """显示凭证错误信息"""
        print("请先运行登录程序获取凭证文件")
        print(f"凭证文件路径: {Config.CREDENTIAL_FILE.absolute()}")
        print("\n按任意键退出...")
        input()

    async def _handle_user_session(self, user_id: str):
        """处理用户会话"""
        # 设置音质偏好
        self.downloader.quality_level = self._ask_quality_preference()

        # 获取歌单
        songlists = await self.downloader.get_user_songlists(user_id)
        if not songlists:
            return

        while True:
            choice = self._show_songlist_menu(user_id, songlists)

            if choice == 'q':
                print("再见!")
                return
            elif choice == '0':
                break
            elif choice == 'all':
                await self._download_all_songlists(songlists, user_id)
                break
            elif choice.isdigit():
                await self._handle_single_songlist(songlists, int(choice) - 1, user_id)

    def _ask_quality_preference(self) -> int:
        """询问音质偏好"""
        print("请选择下载音质:")
        for key, (name, _) in QQMusicDownloader.QUALITY_OPTIONS.items():
            print(f"  {key}. {name}")
        while True:
            choice = input(f"请输入序号 (1-{len(QQMusicDownloader.QUALITY_OPTIONS)}, 默认3): ").strip()
            if choice == '':
                choice = '3'
            try:
                choice_num = int(choice)
                if choice_num in QQMusicDownloader.QUALITY_OPTIONS:
                    name, _ = QQMusicDownloader.QUALITY_OPTIONS[choice_num]
                    print(f"已选择: {name}")
                    return choice_num
            except ValueError:
                pass
            print(f"请输入 1-{len(QQMusicDownloader.QUALITY_OPTIONS)} 之间的数字")

    def _show_songlist_menu(self, user_id: str, songlists: List[Dict]) -> str:
        """显示歌单菜单"""
        print(f"\n当前用户: {user_id}")
        quality_name, _ = QQMusicDownloader.QUALITY_OPTIONS.get(self.downloader.quality_level, QQMusicDownloader.QUALITY_OPTIONS[3])
        print(f"音质模式: {quality_name}")
        print(f"找到 {len(songlists)} 个歌单:")

        for i, sl in enumerate(songlists, 1):
            song_count = sl.get('songNum', 0)
            songlist_name = sl.get('dirName', '未知歌单')
            print(f"  {i}. {songlist_name} (歌曲数: {song_count})")

        return input(
            f"\n请输入歌单编号 (1-{len(songlists)})，输入'all'下载所有歌单，"
            f"输入'0'返回用户选择，输入'q'退出: "
        ).strip()

    async def _download_all_songlists(self, songlists: List[Dict], user_id: str):
        """下载所有歌单"""
        print(f"\n开始下载用户 {user_id} 的所有歌单 (共 {len(songlists)} 个歌单)")
        print("=" * 50)

        total_success = 0
        total_failed = 0

        for i, songlist_info in enumerate(songlists, 1):
            songlist_name = songlist_info.get('dirName', '未知歌单')

            # 跳过无权限的"我喜欢"歌单
            if (songlist_info.get('dirId') == 201 and
                    self.downloader._is_other_user(user_id)):
                print(f"\n{i}/{len(songlists)} 跳过 '我喜欢' 歌单 (权限不足)")
                continue

            print(f"\n{i}/{len(songlists)} 正在处理歌单: {songlist_name}")

            songs = await self.downloader.get_songlist_details(songlist_info, user_id)
            if songs:
                success, failed = await self.downloader.download_songlist(
                    songlist_info, user_id, songs
                )
                total_success += success
                total_failed += failed

        print(f"\n所有歌单下载完成!")
        print(f"总计处理: {len(songlists)} 个歌单")
        print(f"总计下载: {total_success} 首歌曲, 失败: {total_failed} 首")
        print(f"保存位置: {self.downloader.download_dir}")

    async def _handle_single_songlist(self, songlists: List[Dict], index: int, user_id: str):
        """处理单个歌单"""
        if 0 <= index < len(songlists):
            selected_songlist = songlists[index]
            songs = await self.downloader.preview_songlist(selected_songlist, user_id)

            if songs and self._ask_download_confirmation():
                await self.downloader.download_songlist(selected_songlist, user_id, songs)
        else:
            print("无效的选择，请重新输入")

    def _ask_download_confirmation(self) -> bool:
        """询问下载确认"""
        choice = input("\n是否下载这个歌单？(Y/n): ").strip().lower()
        # 回车直接选择 y
        if choice == '':
            choice = 'y'
        return choice == 'y'


async def main():
    """主函数"""
    downloader = QQMusicDownloader()

    try:
        await downloader.initialize()
        interface = InteractiveInterface(downloader)
        await interface.run()
    except Exception as e:
        print(f"程序运行出错: {e}")
        print("\n按任意键退出...")
        input()
    finally:
        await downloader.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n用户中断，程序退出")
    except Exception as e:
        print(f"程序异常: {e}")
        print("按任意键退出...")
        input()