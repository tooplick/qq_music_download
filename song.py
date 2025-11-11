#!/usr/bin/env python3

import asyncio
import pickle
import aiohttp
import aiofiles
from pathlib import Path
from typing import Optional, Literal, Dict, Any, List, Tuple
import logging
import sys
from dataclasses import dataclass
from contextlib import asynccontextmanager

from qqmusic_api import search
from qqmusic_api.song import get_song_urls, SongFileType
from qqmusic_api.login import Credential, check_expired
from qqmusic_api.lyric import get_lyric
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, USLT


## 配置常量
class Config:
    COVER_SIZE = 800 #封面尺寸[150, 300, 500, 800]
    DOWNLOAD_TIMEOUT = 30
    CREDENTIAL_FILE = Path("qqmusic_cred.pkl")
    MUSIC_DIR = Path("./music")
    MIN_FILE_SIZE = 1024
    SEARCH_RESULTS_COUNT = 5  #搜索结果数量


## 日志配置
def setup_logging():
    """配置日志系统"""
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
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

    def __init__(self, credential_file: Path = Config.CREDENTIAL_FILE):
        self.credential_file = credential_file
        self.credential_loaded = False
        self.credential_refreshed = False

    async def load_and_refresh_credential(self) -> Optional[Credential]:
        """加载并刷新凭证"""
        self.credential_loaded = False
        self.credential_refreshed = False

        if not self.credential_file.exists():
            return None

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
                    return None

            self.credential_loaded = True
            return cred

        except Exception as e:
            logger.error(f"加载凭证失败: {e}")
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
            except Exception as e:
                logger.error(f"凭证自动刷新失败: {e}")
        return None


class QQMusicSingleDownloader:
    """QQ音乐单曲下载器"""

    def __init__(self, download_dir: Path = Config.MUSIC_DIR):
        self.download_dir = FileManager.ensure_directory(download_dir)
        self.credential = None
        self.prefer_flac = False

        # 初始化组件
        self.network = NetworkManager()
        self.file_manager = FileManager()
        self.credential_manager = CredentialManager()
        self.metadata_manager = MetadataManager(self.network)

    async def initialize(self):
        """初始化下载器"""
        await self.network.get_session().__aenter__()
        self.credential = await self.credential_manager.load_and_refresh_credential()

    async def close(self):
        """关闭下载器"""
        await self.network.close()

    def get_credential_info(self) -> Tuple[str, bool, bool]:
        """获取凭证文件信息"""
        credential_info = "凭证文件: 不存在"
        if Config.CREDENTIAL_FILE.exists():
            try:
                from datetime import datetime
                file_mtime = datetime.fromtimestamp(Config.CREDENTIAL_FILE.stat().st_mtime)
                credential_info = f"修改时间: {file_mtime.strftime('%Y-%m-%d %H:%M:%S')}"
            except Exception as e:
                credential_info = f"修改时间: 无法获取 ({e})"

        loaded = self.credential_manager.credential_loaded
        refreshed = self.credential_manager.credential_refreshed
        return credential_info, loaded, refreshed

    async def search_songs(self, keyword: str) -> List[Dict[str, Any]]:
        """搜索歌曲"""
        if not keyword:
            raise ValueError("搜索关键词不能为空")

        try:
            results = await search.search_by_type(keyword, num=Config.SEARCH_RESULTS_COUNT)
            if not results:
                raise ValueError("未找到相关歌曲")

            return results
        except Exception as e:
            raise DownloadError(f"搜索失败: {e}")

    def extract_song_info(self, song_data: Dict[str, Any]) -> SongInfo:
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

    def _get_quality_strategy(self) -> List[Tuple[SongFileType, str]]:
        """获取音质下载策略"""
        if self.prefer_flac:
            return [
                (SongFileType.FLAC, "FLAC"),
                (SongFileType.MP3_320, "320kbps"),
                (SongFileType.MP3_128, "128kbps")
            ]
        else:
            return [
                (SongFileType.MP3_320, "320kbps"),
                (SongFileType.MP3_128, "128kbps")
            ]

    async def download_song(self, song_data: Dict[str, Any]) -> bool:
        """下载单首歌曲"""
        try:
            song_info = self.extract_song_info(song_data)

            # 检查VIP歌曲权限
            if song_info.is_vip and not self.credential:
                print("这首歌是VIP歌曲，需要登录才能下载高音质版本")

            safe_filename = self.file_manager.sanitize_filename(
                f"{song_info.singer} - {song_info.name}"
            )

            # 尝试不同音质
            for file_type, quality_name in self._get_quality_strategy():
                file_path = self.download_dir / f"{safe_filename}{file_type.e}"

                if file_path.exists():
                    print(f"文件已存在，跳过: {file_path.name}")
                    return True

                success = await self._download_with_quality(
                    song_info, file_type, quality_name, file_path, song_data
                )
                if success:
                    return True

            print("所有音质下载失败")
            return False

        except Exception as e:
            logger.error(f"下载歌曲失败: {e}")
            return False

    async def _download_with_quality(self, song_info: SongInfo, file_type: SongFileType,
                                     quality_name: str, file_path: Path, song_data: Dict[str, Any]) -> bool:
        """使用指定音质下载"""
        print(f"尝试下载 {quality_name}: {song_info.singer} - {song_info.name}{' [VIP]' if song_info.is_vip else ''}")

        urls = await get_song_urls([song_info.mid], file_type=file_type,
                                   credential=self.credential)
        url = urls.get(song_info.mid)

        if not url:
            print(f"无法获取歌曲URL ({quality_name})")
            return False

        async with self.network.get_session() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    content = await response.read()
                    if len(content) > Config.MIN_FILE_SIZE:
                        await self._save_file(file_path, content)
                        await self._add_metadata(file_path, song_info, song_data)
                        print(f"下载成功: ---> {file_path.name}")
                        return True
                    else:
                        print(f"文件过小，可能下载失败")
                else:
                    print(f"下载失败，状态码: {response.status}")

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
            logger.warning(f"元数据添加失败: {e}")

    async def _get_lyrics(self, song_mid: str) -> Optional[dict]:
        """获取歌词"""
        try:
            return await get_lyric(song_mid)
        except Exception:
            return None


class InteractiveInterface:
    """交互式界面"""

    def __init__(self, downloader: QQMusicSingleDownloader):
        self.downloader = downloader

    async def run(self):
        """运行交互界面"""
        print("QQ音乐单曲下载")
        print("版本号: v2.1.1")  # 更新版本号

        # 初始化下载器
        await self.downloader.initialize()

        # 获取凭证信息
        credential_info, loaded, refreshed = self.downloader.get_credential_info()

        # 显示凭证状态
        if loaded:
            if refreshed:
                print("使用本地凭证登录成功 (已自动刷新)")
            else:
                print("使用本地凭证登录成功")
        else:
            print("未找到凭证文件，仅能下载免费歌曲")

        # 显示凭证文件信息
        print(credential_info)
        print("-" * 50)

        # 询问音质偏好
        self.downloader.prefer_flac = self._ask_quality_preference()

        # 主循环
        while True:
            try:
                await self._search_and_download_loop()
            except KeyboardInterrupt:
                print("\n再见!")
                break
            except Exception as e:
                print(f"发生错误: {e}")
                continue

    def _ask_quality_preference(self) -> bool:
        """询问音质偏好"""
        while True:
            flac_choice = input("你希望下载更高音质吗？(y/n): ").strip().lower()
            if flac_choice in ['y', 'n']:
                prefer_flac = (flac_choice == 'y')
                quality_text = "高品质音质 (FLAC优先)" if prefer_flac else "标准音质 (MP3_320优先)"
                print(f"已选择 {quality_text}")
                return prefer_flac
            else:
                print("请输入 y 或 n")

    async def _search_and_download_loop(self):
        """搜索和下载循环"""
        # 获取搜索关键词
        keyword = ""
        while not keyword:
            keyword = input("请输入要搜索的歌曲 (输入'q'退出): ").strip()
            if keyword.lower() == 'q':
                print("再见!")
                exit(0)
            if not keyword:
                print("歌曲名不能为空，请重新输入")

        # 搜索歌曲
        try:
            results = await self.downloader.search_songs(keyword)
        except Exception as e:
            print(f"搜索失败: {e}")
            return

        # 显示搜索结果
        self._display_search_results(results)

        # 选择歌曲
        selected_song = self._select_song(results)
        if not selected_song:
            return

        # 下载歌曲
        await self.downloader.download_song(selected_song)

    def _display_search_results(self, results: List[Dict[str, Any]]):
        """显示搜索结果"""
        print(f"\n找到 {len(results)} 个结果:")
        print("=" * 60)

        for i, song_data in enumerate(results, 1):
            song_info = self.downloader.extract_song_info(song_data)
            vip_mark = " [VIP]" if song_info.is_vip else ""
            print(f"{i}. {song_info.singer} - {song_info.name}{vip_mark}")

        print("=" * 60)

    def _select_song(self, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """选择歌曲"""
        while True:
            try:
                choice = input(f"请输入要下载的序号 (1-{len(results)}, 输入'q'返回): ").strip()

                if choice.lower() == 'q':
                    return None

                choice_num = int(choice)
                if 1 <= choice_num <= len(results):
                    selected_song = results[choice_num - 1]
                    song_info = self.downloader.extract_song_info(selected_song)
                    vip_mark = " [VIP]" if song_info.is_vip else ""
                    print(f"你选择了: {song_info.singer} - {song_info.name}{vip_mark}")
                    return selected_song
                else:
                    print(f"请输入 1-{len(results)} 之间的数字")
            except ValueError:
                print("请输入有效的数字")
            except KeyboardInterrupt:
                return None


async def main():
    """主函数"""
    downloader = QQMusicSingleDownloader()

    try:
        await downloader.initialize()
        interface = InteractiveInterface(downloader)
        await interface.run()
    except Exception as e:
        print(f"程序运行出错: {e}")
    finally:
        await downloader.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序异常: {e}")