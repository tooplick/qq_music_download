#!/usr/bin/env python3

import asyncio
import pickle
import aiohttp
import aiofiles
from pathlib import Path
from typing import List, Dict, Any, Optional, Literal
import logging
import sys

from qqmusic_api import user, songlist, song
from qqmusic_api.song import get_song_urls, SongFileType
from qqmusic_api.login import Credential, check_expired
from qqmusic_api.lyric import get_lyric
from mutagen.flac import FLAC, Picture

# 配置
CREDENTIAL_FILE = Path("qqmusic_cred.pkl")
MUSIC_DIR = Path("./music")
MUSIC_DIR.mkdir(exist_ok=True)

# 日志配置 - 隐藏HTTP请求日志
logging.basicConfig(
    level=logging.WARNING,  # 改为WARNING级别，隐藏INFO日志
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# 特别设置qqmusic_api的日志级别为WARNING，隐藏HTTP请求日志
logging.getLogger("qqmusic_api").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def get_cover(mid: str, size: Literal[150, 300, 500, 800] = 800) -> str:

    if size not in [150, 300, 500, 800]:
        raise ValueError("not supported size")
    return f"https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{mid}.jpg"


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
            if cover_data and len(cover_data) > 1024:  # 确保不是空图片
                image = Picture()
                image.type = 3  # 封面图片
                # 根据URL判断MIME类型
                if cover_url.lower().endswith('.png'):
                    image.mime = 'image/png'
                else:
                    image.mime = 'image/jpeg'
                image.desc = 'Cover'
                image.data = cover_data

                audio.clear_pictures()
                audio.add_picture(image)
                logger.info(f"已添加封面到 {file_path.name}")

        # 添加歌词
        if lyrics_data:
            lyric_text = lyrics_data.get('lyric', '')
            if lyric_text:
                audio['lyrics'] = lyric_text
                logger.info(f"已添加歌词到 {file_path.name}")

            # 添加翻译歌词（如果有）
            trans_text = lyrics_data.get('trans', '')
            if trans_text:
                audio['translyrics'] = trans_text

        audio.save()
        logger.info(f"已为 {file_path.name} 添加元数据")
        return True

    except Exception as e:
        logger.error(f"添加元数据失败: {e}")
        return False


async def download_file_content(url: str) -> Optional[bytes]:
    """异步下载文件内容"""
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    # 检查内容是否有效（大于1KB）
                    if len(content) > 1024:
                        return content
                    else:
                        logger.warning(f"下载内容过小: {len(content)} bytes")
                else:
                    logger.warning(f"下载失败，状态码: {resp.status}")
                return None
    except Exception as e:
        logger.error(f"下载文件时出错: {e}")
        return None


class OthersSonglistDownloader:

    def __init__(self, download_dir: Path = MUSIC_DIR):
        self.download_dir = download_dir
        self.download_dir.mkdir(exist_ok=True)
        self.credential = None
        self.session = None
        self.prefer_flac = False  # 默认不使用FLAC

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
            return None

        try:
            with CREDENTIAL_FILE.open("rb") as f:
                cred: Credential = pickle.load(f)

            # 检查是否过期
            is_expired = await check_expired(cred)

            if is_expired:
                print("登录凭证已过期，尝试自动刷新...")

                # 检查是否可以刷新
                can_refresh = await cred.can_refresh()
                if can_refresh:
                    try:
                        await cred.refresh()
                        # 保存刷新后的凭证
                        with CREDENTIAL_FILE.open("wb") as f:
                            pickle.dump(cred, f)
                        print("凭证自动刷新成功!")
                        return cred
                    except Exception as refresh_error:
                        print(f"凭证自动刷新失败: {refresh_error}")
                        return None
                else:
                    print("凭证不支持刷新，无法继续")
                    return None
            else:
                print("使用本地凭证登录成功!")
                return cred

        except Exception as e:
            print(f"加载凭证失败: {e}")
            return None

    async def get_others_songlists(self, target_musicid: str) -> List[Dict[str, Any]]:
        """获取歌单列表"""
        if not self.credential:
            print("未登录，无法获取歌单")
            return []

        try:
            # 获取歌单列表
            print(f"正在查询用户 {target_musicid} 的歌单...")
            songlists = await user.get_created_songlist(target_musicid, credential=self.credential)

            if not songlists:
                print("未找到该用户的歌单或歌单为空")
                return []

            return songlists

        except Exception as e:
            print(f"获取歌单失败，可能是用户不存在或网络问题: {e}")
            return []

    async def get_songlist_songs(self, songlist_info: Dict[str, Any], target_musicid: str) -> List[Dict[str, Any]]:
        """获取歌单中的所有歌曲"""
        if not self.credential:
            print("未登录，无法获取歌曲")
            return []

        try:
            # 使用正确的参数获取歌单歌曲
            dirid = songlist_info.get('dirId', 0)
            tid = songlist_info.get('tid', 0)

            # 对于"我喜欢"歌单(dirId=201)，使用特殊参数
            if dirid == 201:
                # 检查权限：只有凭证对应的用户才能查看自己的"我喜欢"歌单
                if self.credential and hasattr(self.credential, 'musicid'):
                    if str(self.credential.musicid) != str(target_musicid):
                        print("权限不足!收藏歌单不公开!!")
                        return []

                songs = await songlist.get_songlist(0, dirid)
            else:
                songs = await songlist.get_songlist(tid, 0)

            print(f"歌单中有 {len(songs)} 首歌曲")
            return songs

        except Exception as e:
            print(f"获取歌单歌曲失败: {e}")
            return []

    async def extract_song_info(self, song_data: Dict[str, Any]) -> Dict[str, Any]:
        """从歌曲数据中提取所需信息"""
        # 获取歌曲名称
        song_name = song_data.get('title', '未知歌曲')

        # 获取歌手信息
        singer_info = song_data.get('singer', [])
        if isinstance(singer_info, list) and len(singer_info) > 0:
            singer_name = singer_info[0].get('name', '未知歌手')
        else:
            singer_name = '未知歌手'

        # 获取歌曲mid
        song_mid = song_data.get('mid', '')

        # 检查是否为VIP歌曲
        is_vip = song_data.get('pay', {}).get('pay_play', 0) != 0

        # 获取专辑信息
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

    async def download_song_with_fallback(self, song_data: Dict[str, Any], folder: Path) -> bool:
        """下载单首歌曲，根据音质偏好进行降级下载"""
        if not self.credential:
            print("未登录，无法下载歌曲")
            return False

        try:
            # 提取歌曲信息
            song_info = await self.extract_song_info(song_data)
            song_mid = song_info['songmid']
            song_name = song_info['songname']
            singer_name = song_info['singer'][0]['name']
            is_vip = song_info['is_vip']
            album_mid = song_info['album_mid']
            album_name = song_info['album_name']

            # 如果无法获取歌曲mid，跳过下载
            if not song_mid:
                print(f"!无法获取歌曲MID: {song_name}")
                return False

            # 清理文件名中的非法字符
            safe_filename = self.sanitize_filename(f"{singer_name} - {song_name}")

            # 根据音质偏好设置下载策略
            if self.prefer_flac:
                # FLAC优先策略：FLAC -> MP3_320 -> MP3_128
                quality_order = [
                    (SongFileType.FLAC, "FLAC"),
                    (SongFileType.MP3_320, "320kbps"),
                    (SongFileType.MP3_128, "128kbps")
                ]
            else:
                # MP3优先策略：MP3_320 -> MP3_128
                quality_order = [
                    (SongFileType.MP3_320, "320kbps"),
                    (SongFileType.MP3_128, "128kbps")
                ]

            # 尝试不同音质
            downloaded_file_type = None
            for file_type, quality_name in quality_order:
                file_path = folder / f"{safe_filename}{file_type.e}"

                # 如果文件已存在，跳过下载
                if file_path.exists():
                    print(f"文件已存在，跳过: {safe_filename} ({quality_name})")
                    downloaded_file_type = file_type
                    return True

                print(f">尝试下载 {quality_name}: {safe_filename}{' [VIP]' if is_vip else ''}")

                # 获取歌曲URL
                urls = await get_song_urls([song_mid], file_type=file_type, credential=self.credential)
                url = urls.get(song_mid)

                if not url:
                    print(f"!无法获取歌曲URL ({quality_name}): {song_name}")
                    continue

                # 下载歌曲
                async with self.session.get(url) as response:
                    if response.status == 200:
                        content = await response.read()
                        # 检查文件是否有效
                        if len(content) > 1024:
                            async with aiofiles.open(file_path, 'wb') as f:
                                await f.write(content)
                            print(f"-->下载成功 ({quality_name}): {safe_filename}")
                            downloaded_file_type = file_type

                            # 为FLAC文件自动添加元数据（不再询问）
                            if downloaded_file_type == SongFileType.FLAC and file_path.suffix.lower() == '.flac':
                                try:
                                    # 获取封面URL
                                    cover_url = None
                                    if album_mid:
                                        cover_url = get_cover(album_mid, 800)  # 使用800px大小的封面

                                    # 获取歌词
                                    lyrics_data = None
                                    try:
                                        lyrics_data = await get_lyric(song_mid)
                                    except Exception as e:
                                        print(f"!获取歌词失败: {e}")

                                    # 添加元数据到FLAC文件
                                    if cover_url or lyrics_data:
                                        metadata_success = await add_metadata_to_flac(
                                            file_path,
                                            song_info,
                                            cover_url,
                                            lyrics_data
                                        )
                                        if metadata_success:
                                            print(f"  已自动添加元数据(封面800px+歌词): {safe_filename}")
                                        else:
                                            print(f"!添加元数据失败: {safe_filename}")
                                    else:
                                        print(f"!无法获取元数据: {safe_filename}")

                                except Exception as e:
                                    print(f"!处理元数据失败: {e}")

                            return True
                        else:
                            print(f"!{quality_name}文件过小，可能下载失败: {song_name}")
                    else:
                        print(f"!{quality_name}下载失败: {song_name}, 状态码: {response.status}")

            # 所有音质都尝试失败
            print(f"所有音质下载失败: {song_name}")
            return False

        except Exception as e:
            print(f"下载歌曲失败 {song_data.get('name', '未知歌曲')}: {e}")
            return False

    def sanitize_filename(self, filename: str) -> str:
        """清理文件名中的非法字符"""
        # Windows文件名非法字符
        illegal_chars = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
        for char in illegal_chars:
            filename = filename.replace(char, '_')
        return filename

    async def preview_songlist_songs(self, songlist_info: Dict[str, Any], target_musicid: str) -> List[Dict[str, Any]]:
        """预览歌单歌曲（不下载）"""
        print(f"\n正在获取歌单歌曲列表...")
        songs = await self.get_songlist_songs(songlist_info, target_musicid)

        if not songs:
            print("无法获取歌单歌曲或歌单为空")
            # 添加回车继续
            input("按回车键继续...")
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
        if not self.credential:
            print("未登录，无法下载歌单")
            input("按回车键继续...")
            return

        songlist_name = songlist_info.get('dirName', '未知歌单')

        # 创建歌单文件夹（包含用户ID避免冲突）
        safe_folder_name = self.sanitize_filename(f"用户{target_musicid}_{songlist_name}")
        folder = self.download_dir / safe_folder_name
        folder.mkdir(exist_ok=True)

        # 显示下载音质信息
        quality_info = "FLAC -> MP3_320 -> MP3_128" if self.prefer_flac else "MP3_320 -> MP3_128"
        metadata_info = " (FLAC文件自动添加封面800px+歌词)" if self.prefer_flac else ""
        print(f"\n开始下载歌单: {songlist_name} (共 {len(songs)} 首歌曲)")
        print(f"下载音质策略: {quality_info}{metadata_info}")

        # 创建下载任务（限制并发数量）
        success_count = 0
        failed_count = 0
        batch_size = 3  # 每次并发下载3首

        for i in range(0, len(songs), batch_size):
            batch = songs[i:i + batch_size]
            tasks = [self.download_song_with_fallback(song, folder) for song in batch]
            results = await asyncio.gather(*tasks)

            for result in results:
                if result:
                    success_count += 1
                else:
                    failed_count += 1

            # 显示进度
            total_done = i + len(batch)
            progress = int((total_done / len(songs)) * 100)
            print(f"进度: {total_done}/{len(songs)} ({progress}%) - 成功: {success_count}, 失败: {failed_count}")

            # 延迟一下，避免请求过于频繁
            if i + batch_size < len(songs):
                await asyncio.sleep(1)

        print(f"\n歌单下载完成:{songlist_name}")
        print(f"总计: {len(songs)} 首, 成功: {success_count} 首, 失败: {failed_count} 首")
        print(f"保存位置:==>{folder}")
        # 添加回车继续
        input("按回车键继续...")

    async def interactive_download(self):
        """交互式下载界面"""
        print("QQ音乐歌单下载器")
        print("版本号:v2.0.4")
        print("-" * 50)

        # 加载凭证（包含自动刷新功能）
        self.credential = await self.load_and_refresh_credential()

        # 如果没有凭证，直接提示并退出
        if not self.credential:
            print("请登录获得凭证继续!!!")
            input("按回车键退出...")
            return

        while True:
            try:
                # 输入目标用户musicid
                print("-" * 50)
                target_musicid = input("请输入你的musicid (输入'q'退出): ").strip()

                if target_musicid.lower() == 'q':
                    print("Bye")
                    break

                if not target_musicid:
                    print("musicid不能为空!!!")
                    continue

                # 询问音质偏好
                flac_choice = input("你希望更高音质吗？(y/n): ").strip().lower()

                if flac_choice == 'y':
                    self.prefer_flac = True
                    print("已选择高品质音质 (FLAC优先，自动添加封面800px+歌词)")
                else:
                    self.prefer_flac = False
                    print("已选择标准音质 (MP3_320优先)")

                # 获取他人歌单
                songlists = await self.get_others_songlists(target_musicid)
                if not songlists:
                    continue

                # 在当前用户下循环选择歌单下载
                while True:
                    print(f"\n当前用户: {target_musicid}")
                    print(
                        f"音质模式: {'高品质 (FLAC优先，自动添加封面800px+歌词)' if self.prefer_flac else '标准 (MP3_320优先)'}")
                    print(f"🎵 找到 {len(songlists)} 个歌单:")
                    for i, sl in enumerate(songlists, 1):
                        song_count = sl.get('songNum', 0)
                        songlist_name = sl.get('dirName', '未知歌单')
                        print(f"  {i}. {songlist_name} (歌曲数: {song_count})")

                    choice = input(f"\n请输入歌单编号 (1-{len(songlists)})，输入'0'返回用户选择，输入'q'退出: ").strip()

                    if choice.lower() == 'q':
                        print("Bye")
                        return
                    elif choice == '0':
                        break

                    try:
                        idx = int(choice) - 1
                        if 0 <= idx < len(songlists):
                            selected_songlist = songlists[idx]

                            # 先预览歌单歌曲
                            songs = await self.preview_songlist_songs(selected_songlist, target_musicid)

                            if songs:
                                # 询问用户是否下载
                                download_choice = input(f"\n是否下载这个歌单？(y/n): ").strip().lower()
                                if download_choice == 'y':
                                    await self.download_songlist(selected_songlist, target_musicid, songs)
                                else:
                                    print("取消下载，返回歌单选择")
                        else:
                            print("无效的选择，请重新输入")
                    except ValueError:
                        print("请输入有效的数字")

            except KeyboardInterrupt:
                print("Bye")
                break


async def main():
    """主函数"""
    downloader = OthersSonglistDownloader()

    try:
        await downloader.initialize()
        await downloader.interactive_download()
    except Exception as e:
        print(f"程序运行出错: {e}")
    finally:
        await downloader.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n 用户中断，程序退出")