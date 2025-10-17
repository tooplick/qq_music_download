#!/usr/bin/env python3

import asyncio
import pickle
import aiohttp
import aiofiles
from pathlib import Path
from typing import List, Dict, Any, Optional
import logging
import sys

from qqmusic_api import user, songlist, song
from qqmusic_api.song import get_song_urls, SongFileType
from qqmusic_api.login import Credential, check_expired

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

    async def load_credential(self) -> Optional[Credential]:
        """加载本地登录凭证"""
        if not CREDENTIAL_FILE.exists():
            return None

        try:
            with CREDENTIAL_FILE.open("rb") as f:
                cred: Credential = pickle.load(f)

            if await check_expired(cred):
                print("登录凭证已过期，请重新登录")
                return None

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
        song_name = song_data.get('name', '未知歌曲')

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

        return {
            'songname': song_name,
            'singer': [{'name': singer_name}],
            'songmid': song_mid,
            'is_vip': is_vip
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
            for file_type, quality_name in quality_order:
                file_path = folder / f"{safe_filename}{file_type.e}"

                # 如果文件已存在，跳过下载
                if file_path.exists():
                    print(f"文件已存在，跳过: {safe_filename} ({quality_name})")
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
        print(f"\n开始下载歌单: {songlist_name} (共 {len(songs)} 首歌曲)")
        print(f"下载音质策略: {quality_info}")

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
        print("版本号:v2.0.1")
        print("-" * 50)

        # 加载凭证
        self.credential = await self.load_credential()

        # 如果没有凭证，直接提示并退出
        if not self.credential:
            print("请登录获得凭证继续!!!")
            input("按回车键退出...")
            return

        while True:
            try:
                # 输入目标用户musicid
                print("\n" + "-" * 30)
                target_musicid = input("请输入你的musicid (输入'q'退出): ").strip()

                if target_musicid.lower() == 'q':
                    print("Bye")
                    break

                if not target_musicid:
                    print("musicid不能为空!!!")
                    continue

                # 询问音质偏好
                print("\n" + "-" * 30)
                flac_choice = input("你希望更高音质吗？(y/n): ").strip().lower()

                if flac_choice == 'y':
                    self.prefer_flac = True
                    print("已选择高品质音质 (FLAC优先)")
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
                    print(f"音质模式: {'高品质 (FLAC优先)' if self.prefer_flac else '标准 (MP3_320优先)'}")
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