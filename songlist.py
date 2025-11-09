#!/usr/bin/env python3

import asyncio
import pickle
import aiohttp
import aiofiles
from pathlib import Path
from typing import List, Dict, Any, Optional, Literal
import logging
import sys
import os

from qqmusic_api import user, songlist, song
from qqmusic_api.song import get_song_urls, SongFileType
from qqmusic_api.login import Credential, check_expired
from qqmusic_api.lyric import get_lyric
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, USLT

## 配置
# 并发数量
batch_size = 3

# 封面尺寸配置[150, 300, 500, 800]
cover_size = 800

CREDENTIAL_FILE = Path("qqmusic_cred.pkl")
MUSIC_DIR = Path("./music")
MUSIC_DIR.mkdir(exist_ok=True)

# 日志配置 - 隐藏HTTP请求日志
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# 特别设置qqmusic_api的日志级别为WARNING，隐藏HTTP请求日志
logging.getLogger("qqmusic_api").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def get_cover(mid: str, size: Literal[150, 300, 500, 800] = 800) -> str:
    """获取封面URL"""
    if size not in [150, 300, 500, 800]:
        raise ValueError("not supported size")
    return f"https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{mid}.jpg"


async def download_file_content(url: str) -> Optional[bytes]:
    """异步下载文件内容"""
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    if len(content) > 1024:  # 检查内容是否有效
                        return content
                    else:
                        logger.warning(f"下载内容过小: {len(content)} bytes")
                else:
                    logger.warning(f"下载失败，状态码: {resp.status}")
                return None
    except Exception as e:
        logger.error(f"下载文件时出错: {e}")
        return None


async def add_metadata_to_flac(file_path: Path, song_info: dict, cover_url: str = None, lyrics_data: dict = None):
    """为FLAC文件添加封面和歌词"""
    try:
        audio = FLAC(file_path)

        # 添加基本元数据
        audio['title'] = song_info.get('songname', '')
        audio['artist'] = song_info.get('singer', [{}])[0].get('name', '')
        audio['album'] = song_info.get('album_name', '')

        # 添加封面
        if cover_url:
            cover_data = await download_file_content(cover_url)
            if cover_data and len(cover_data) > 1024:
                image = Picture()
                image.type = 3  # 封面图片
                if cover_url.lower().endswith('.png'):
                    image.mime = 'image/png'
                else:
                    image.mime = 'image/jpeg'
                image.desc = 'Cover'
                image.data = cover_data

                audio.clear_pictures()
                audio.add_picture(image)

        # 添加歌词
        if lyrics_data:
            lyric_text = lyrics_data.get('lyric', '')
            if lyric_text:
                audio['lyrics'] = lyric_text

            trans_text = lyrics_data.get('trans', '')
            if trans_text:
                audio['translyrics'] = trans_text

        audio.save()
        return True

    except Exception as e:
        logger.error(f"添加元数据失败: {e}")
        return False


async def add_metadata_to_mp3(file_path: Path, song_info: dict, cover_url: str = None, lyrics_data: dict = None):
    """为MP3文件添加封面和歌词"""
    try:
        audio = ID3(file_path)

        # 添加基本元数据
        audio['TIT2'] = TIT2(encoding=3, text=song_info.get('songname', ''))
        audio['TPE1'] = TPE1(encoding=3, text=song_info.get('singer', [{}])[0].get('name', ''))
        audio['TALB'] = TALB(encoding=3, text=song_info.get('album_name', ''))

        # 添加封面
        if cover_url:
            cover_data = await download_file_content(cover_url)
            if cover_data and len(cover_data) > 1024:
                if cover_url.lower().endswith('.png'):
                    mime_type = 'image/png'
                else:
                    mime_type = 'image/jpeg'

                audio['APIC'] = APIC(
                    encoding=3,
                    mime=mime_type,
                    type=3,
                    desc='Cover',
                    data=cover_data
                )

        # 添加歌词
        if lyrics_data:
            lyric_text = lyrics_data.get('lyric', '')
            if lyric_text:
                audio['USLT'] = USLT(encoding=3, lang='eng', desc='Lyrics', text=lyric_text)

        audio.save()
        return True

    except Exception as e:
        logger.error(f"添加MP3元数据失败: {e}")
        return False


class OthersSonglistDownloader:
    """QQ音乐歌单下载器"""

    def __init__(self, download_dir: Path = MUSIC_DIR):
        self.download_dir = download_dir
        self.download_dir.mkdir(exist_ok=True)
        self.credential = None
        self.session = None
        self.prefer_flac = False

    async def initialize(self):
        """初始化会话"""
        self.session = aiohttp.ClientSession()

    async def close(self):
        """关闭会话"""
        if self.session:
            await self.session.close()

    async def load_and_refresh_credential(self) -> Optional[Credential]:
        """加载本地登录凭证，如果过期则自动刷新"""
        if not CREDENTIAL_FILE.exists():
            print(" 未找到登录凭证文件")
            return None

        try:
            with CREDENTIAL_FILE.open("rb") as f:
                cred: Credential = pickle.load(f)

            # 检查是否过期
            is_expired = await check_expired(cred)

            if is_expired:
                print(" 登录凭证已过期，尝试自动刷新...")

                can_refresh = await cred.can_refresh()
                if can_refresh:
                    try:
                        await cred.refresh()
                        with CREDENTIAL_FILE.open("wb") as f:
                            pickle.dump(cred, f)
                        print(" 凭证自动刷新成功!")
                        return cred
                    except Exception as refresh_error:
                        print(f" 凭证自动刷新失败: {refresh_error}")
                        return None
                else:
                    print(" 凭证不支持刷新，无法继续")
                    return None
            else:
                print("使用本地凭证登录成功!")
                return cred

        except Exception as e:
            print(f"❌ 加载凭证失败: {e}")
            return None

    def check_credential(self) -> bool:
        """检查凭证是否存在"""
        if not self.credential:
            print("\n" + "="*50)
            print(" 错误：未检测到登录凭证！")
            print("请先运行登录程序获取凭证文件")
            print(f"凭证文件路径: {CREDENTIAL_FILE.absolute()}")
            print("="*50)
            return False
        return True

    async def get_others_songlists(self, target_musicid: str) -> List[Dict[str, Any]]:
        """获取歌单列表"""
        if not self.check_credential():
            return []

        try:
            print(f" 正在查询用户 {target_musicid} 的歌单...")
            songlists = await user.get_created_songlist(target_musicid, credential=self.credential)

            if not songlists:
                print(" 未找到该用户的歌单或歌单为空")
                return []

            return songlists

        except Exception as e:
            print(f" 获取歌单失败: {e}")
            return []

    async def get_songlist_songs(self, songlist_info: Dict[str, Any], target_musicid: str) -> List[Dict[str, Any]]:
        """获取歌单中的所有歌曲"""
        if not self.check_credential():
            return []

        try:
            dirid = songlist_info.get('dirId', 0)
            tid = songlist_info.get('tid', 0)

            # 对于"我喜欢"歌单的特殊处理
            if dirid == 201:
                if self.credential and hasattr(self.credential, 'musicid'):
                    if str(self.credential.musicid) != str(target_musicid):
                        print("❌ 权限不足!收藏歌单不公开!!")
                        return []

                songs = await songlist.get_songlist(0, dirid)
            else:
                songs = await songlist.get_songlist(tid, 0)

            print(f" 歌单中有 {len(songs)} 首歌曲")
            return songs

        except Exception as e:
            print(f" 获取歌单歌曲失败: {e}")
            return []

    async def extract_song_info(self, song_data: Dict[str, Any]) -> Dict[str, Any]:
        """从歌曲数据中提取所需信息"""
        song_name = song_data.get('title', '未知歌曲')

        singer_info = song_data.get('singer', [])
        if isinstance(singer_info, list) and len(singer_info) > 0:
            singer_name = singer_info[0].get('name', '未知歌手')
        else:
            singer_name = '未知歌手'

        song_mid = song_data.get('mid', '')
        is_vip = song_data.get('pay', {}).get('pay_play', 0) != 0

        album_info = song_data.get('album', {})
        album_name = album_info.get('name', '')
        album_mid = album_info.get('mid', '')

        return {
            'songname': song_name,
            'singer': [{'name': singer_name}],
            'songmid': song_mid,
            'is_vip': is_vip,
            'album_name': album_name,
            'album_mid': album_mid
        }

    def sanitize_filename(self, filename: str) -> str:
        """清理文件名中的非法字符"""
        illegal_chars = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
        for char in illegal_chars:
            filename = filename.replace(char, '_')
        return filename

    async def download_song_with_fallback(self, song_data: Dict[str, Any], folder: Path) -> bool:
        """下载单首歌曲，根据音质偏好进行降级下载"""
        if not self.check_credential():
            return False

        try:
            song_info = await self.extract_song_info(song_data)
            song_mid = song_info['songmid']
            song_name = song_info['songname']
            singer_name = song_info['singer'][0]['name']
            is_vip = song_info['is_vip']
            album_mid = song_info['album_mid']
            album_name = song_info['album_name']

            if not song_mid:
                print(f" 无法获取歌曲MID: {song_name}")
                return False

            safe_filename = self.sanitize_filename(f"{singer_name} - {song_name}")

            # 设置下载策略
            if self.prefer_flac:
                quality_order = [
                    (SongFileType.FLAC, "FLAC"),
                    (SongFileType.MP3_320, "320kbps"),
                    (SongFileType.MP3_128, "128kbps")
                ]
            else:
                quality_order = [
                    (SongFileType.MP3_320, "320kbps"),
                    (SongFileType.MP3_128, "128kbps")
                ]

            # 尝试不同音质
            downloaded_file_type = None
            for file_type, quality_name in quality_order:
                file_path = folder / f"{safe_filename}{file_type.e}"

                if file_path.exists():
                    print(f" 文件已存在，跳过: {safe_filename} ({quality_name})")
                    downloaded_file_type = file_type
                    return True

                print(f" 尝试下载 {quality_name}: {safe_filename}{' [VIP]' if is_vip else ''}")

                urls = await get_song_urls([song_mid], file_type=file_type, credential=self.credential)
                url = urls.get(song_mid)

                if not url:
                    print(f"❌ 无法获取歌曲URL ({quality_name}): {song_name}")
                    continue

                async with self.session.get(url) as response:
                    if response.status == 200:
                        content = await response.read()
                        if len(content) > 1024:
                            async with aiofiles.open(file_path, 'wb') as f:
                                await f.write(content)
                            print(f" 下载成功 ({quality_name}): {safe_filename}")
                            downloaded_file_type = file_type

                            # 自动添加元数据
                            try:
                                cover_url = None
                                if album_mid:
                                    cover_url = get_cover(album_mid, cover_size)

                                lyrics_data = None
                                try:
                                    lyrics_data = await get_lyric(song_mid)
                                except Exception:
                                    pass

                                if cover_url or lyrics_data:
                                    if downloaded_file_type == SongFileType.FLAC and file_path.suffix.lower() == '.flac':
                                        await add_metadata_to_flac(
                                            file_path, song_info, cover_url, lyrics_data
                                        )
                                    elif file_path.suffix.lower() in ['.mp3', '.m4a']:
                                        await add_metadata_to_mp3(
                                            file_path, song_info, cover_url, lyrics_data
                                        )

                            except Exception:
                                pass

                            return True
                        else:
                            print(f" {quality_name}文件过小，可能下载失败: {song_name}")
                    else:
                        print(f" {quality_name}下载失败: {song_name}, 状态码: {response.status}")

            print(f" 所有音质下载失败: {song_name}")
            return False

        except Exception as e:
            print(f" 下载歌曲失败 {song_data.get('name', '未知歌曲')}: {e}")
            return False

    async def preview_songlist_songs(self, songlist_info: Dict[str, Any], target_musicid: str) -> List[Dict[str, Any]]:
        """预览歌单歌曲（不下载）"""
        print(f"\n🔍 正在获取歌单歌曲列表...")
        songs = await self.get_songlist_songs(songlist_info, target_musicid)

        if not songs:
            print(" 无法获取歌单歌曲或歌单为空")
            return []

        print(f"\n🎵 歌单 '{songlist_info.get('dirName', '未知歌单')}' 包含以下 {len(songs)} 首歌曲:")
        print("=" * 60)

        for i, song_data in enumerate(songs, 1):
            song_info = await self.extract_song_info(song_data)
            song_name = song_info['songname']
            singer_name = song_info['singer'][0]['name']
            is_vip = song_info['is_vip']

            vip_mark = " [VIP]" if is_vip else ""
            print(f"{i:2d}. {singer_name} - {song_name}{vip_mark}")

        print("=" * 60)
        return songs

    async def download_songlist(self, songlist_info: Dict[str, Any], target_musicid: str, songs: List[Dict[str, Any]]):
        """下载歌单"""
        if not self.check_credential():
            return

        songlist_name = songlist_info.get('dirName', '未知歌单')

        safe_folder_name = self.sanitize_filename(f"用户{target_musicid}_{songlist_name}")
        folder = self.download_dir / safe_folder_name
        folder.mkdir(exist_ok=True)

        quality_info = "FLAC -> MP3_320 -> MP3_128" if self.prefer_flac else "MP3_320 -> MP3_128"
        metadata_info = " (自动添加封面歌词)"
        print(f"\n 开始下载歌单: {songlist_name} (共 {len(songs)} 首歌曲)")
        print(f"🎵 下载音质策略: {quality_info}{metadata_info}")

        success_count = 0
        failed_count = 0

        for i in range(0, len(songs), batch_size):
            batch = songs[i:i + batch_size]
            tasks = [self.download_song_with_fallback(song, folder) for song in batch]
            results = await asyncio.gather(*tasks)

            for result in results:
                if result:
                    success_count += 1
                else:
                    failed_count += 1

            total_done = i + len(batch)
            progress = int((total_done / len(songs)) * 100)
            print(f" 进度: {total_done}/{len(songs)} ({progress}%) - 成功: {success_count}, 失败: {failed_count}")

            if i + batch_size < len(songs):
                await asyncio.sleep(1)

        print(f"\n 歌单下载完成: {songlist_name}")
        print(f" 总计: {len(songs)} 首, 成功: {success_count} 首, 失败: {failed_count} 首")
        print(f" 保存位置: {folder}")

    async def download_all_songlists(self, songlists: List[Dict[str, Any]], target_musicid: str):
        """下载所有歌单"""
        if not self.check_credential():
            return

        print(f"\n 开始下载用户 {target_musicid} 的所有歌单 (共 {len(songlists)} 个歌单)")
        print("=" * 50)

        total_success = 0
        total_failed = 0

        for i, songlist_info in enumerate(songlists, 1):
            songlist_name = songlist_info.get('dirName', '未知歌单')
            dirid = songlist_info.get('dirId', 0)

            if dirid == 201:
                if self.credential and hasattr(self.credential, 'musicid'):
                    if str(self.credential.musicid) != str(target_musicid):
                        print(f"\n{i}/{len(songlists)} 跳过 '我喜欢' 歌单 (权限不足)")
                        continue

            print(f"\n{i}/{len(songlists)} 正在处理歌单: {songlist_name}")

            songs = await self.get_songlist_songs(songlist_info, target_musicid)
            if not songs:
                print(f"   歌单为空或无法获取歌曲")
                continue

            await self.download_songlist(songlist_info, target_musicid, songs)
            total_success += len(songs)

        print(f"\n 所有歌单下载完成!")
        print(f" 总计处理: {len(songlists)} 个歌单")
        print(f" 总计下载: {total_success} 首歌曲")
        print(f" 保存位置: {self.download_dir}")

    async def interactive_download(self):
        """交互式下载界面"""
        print("QQ音乐歌单下载")
        print("版本号: v2.0.4")
        print("-" * 50)

        # 加载凭证
        self.credential = await self.load_and_refresh_credential()

        # 检查凭证是否存在
        if not self.credential:
            print("\n" + "="*50)
            print(" 错误：未检测到登录凭证！")
            print("请先运行登录程序获取凭证文件")
            print(f"凭证文件路径: {CREDENTIAL_FILE.absolute()}")
            print("\n按任意键退出...")
            input()
            return

        while True:
            try:
                print("-" * 50)
                target_musicid = input("请输入你的musicid (输入'q'退出): ").strip()

                if target_musicid.lower() == 'q':
                    print(" Bye")
                    break

                if not target_musicid:
                    print(" musicid不能为空!!!")
                    continue

                # 询问音质偏好
                flac_choice = input("你希望更高音质吗？(y/n): ").strip().lower()
                self.prefer_flac = (flac_choice == 'y')
                print(f" 已选择 {'高品质音质 (FLAC优先)' if self.prefer_flac else '标准音质 (MP3_320优先)'}")

                # 获取歌单
                songlists = await self.get_others_songlists(target_musicid)
                if not songlists:
                    continue

                # 在当前用户下循环选择歌单下载
                while True:
                    print(f"\n 当前用户: {target_musicid}")
                    print(f" 音质模式: {'高品质 (FLAC优先)' if self.prefer_flac else '标准 (MP3_320优先)'}")
                    print(f" 找到 {len(songlists)} 个歌单:")
                    for i, sl in enumerate(songlists, 1):
                        song_count = sl.get('songNum', 0)
                        songlist_name = sl.get('dirName', '未知歌单')
                        print(f"  {i}. {songlist_name} (歌曲数: {song_count})")

                    choice = input(
                        f"\n请输入歌单编号 (1-{len(songlists)})，输入'all'下载所有歌单，输入'0'返回用户选择，输入'q'退出: ").strip()

                    if choice.lower() == 'q':
                        print(" Bye")
                        return
                    elif choice == '0':
                        break
                    elif choice.lower() == 'all':
                        await self.download_all_songlists(songlists, target_musicid)
                        break

                    try:
                        idx = int(choice) - 1
                        if 0 <= idx < len(songlists):
                            selected_songlist = songlists[idx]

                            songs = await self.preview_songlist_songs(selected_songlist, target_musicid)

                            if songs:
                                download_choice = input(f"\n是否下载这个歌单？(y/n): ").strip().lower()
                                if download_choice == 'y':
                                    await self.download_songlist(selected_songlist, target_musicid, songs)
                                else:
                                    print(" 取消下载，返回歌单选择")
                        else:
                            print(" 无效的选择，请重新输入")
                    except ValueError:
                        print(" 请输入有效的数字")

            except KeyboardInterrupt:
                print("\n Bye")
                break


async def main():
    """主函数"""
    downloader = OthersSonglistDownloader()

    try:
        await downloader.initialize()
        await downloader.interactive_download()
    except Exception as e:
        print(f" 程序运行出错: {e}")
        print("\n按任意键退出...")
        input()
    finally:
        await downloader.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n 用户中断，程序退出")
    except Exception as e:
        print(f"\n 程序异常: {e}")
        print("按任意键退出...")
        input()