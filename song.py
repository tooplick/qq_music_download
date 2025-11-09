import asyncio
import pickle
from pathlib import Path
from qqmusic_api import search
from qqmusic_api.song import get_song_urls, SongFileType
from qqmusic_api.login import Credential, check_expired
from qqmusic_api.lyric import get_lyric
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, USLT
import aiohttp
from typing import Optional, Literal


## 配置
#封面尺寸配置[150, 300, 500, 800]
cover_size = 800

CREDENTIAL_FILE = Path("qqmusic_cred.pkl")
MUSIC_DIR = Path("./music")
MUSIC_DIR.mkdir(exist_ok=True)


def get_cover(mid: str, size: Literal[150, 300, 500, 800] = 800) -> str:
    if size not in [150, 300, 500, 800]:
        raise ValueError("not supported size")
    return f"https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{mid}.jpg"


async def add_metadata_to_flac(file_path: Path, song_info: dict, cover_url: str = None, lyrics_data: dict = None):
    """为FLAC文件添加封面和歌词"""
    try:
        audio = FLAC(file_path)

        # 添加基本元数据
        audio['title'] = song_info.get('title', '')
        audio['artist'] = song_info.get('singer', [{}])[0].get('name', '')
        audio['album'] = song_info.get('album', {}).get('name', '')

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

        # 添加歌词
        if lyrics_data:
            lyric_text = lyrics_data.get('lyric', '')
            if lyric_text:
                audio['lyrics'] = lyric_text

            # 添加翻译歌词（如果有）
            trans_text = lyrics_data.get('trans', '')
            if trans_text:
                audio['translyrics'] = trans_text

        audio.save()
        return True

    except Exception as e:
        print(f"!添加元数据失败: {e}")
        return False


async def add_metadata_to_mp3(file_path: Path, song_info: dict, cover_url: str = None, lyrics_data: dict = None):
    """为MP3文件添加封面和歌词"""
    try:
        audio = ID3(file_path)

        # 添加基本元数据
        audio['TIT2'] = TIT2(encoding=3, text=song_info.get('title', ''))
        audio['TPE1'] = TPE1(encoding=3, text=song_info.get('singer', [{}])[0].get('name', ''))
        audio['TALB'] = TALB(encoding=3, text=song_info.get('album', {}).get('name', ''))

        # 添加封面
        if cover_url:
            cover_data = await download_file_content(cover_url)
            if cover_data and len(cover_data) > 1024:
                # 根据URL判断MIME类型
                if cover_url.lower().endswith('.png'):
                    mime_type = 'image/png'
                else:
                    mime_type = 'image/jpeg'

                audio['APIC'] = APIC(
                    encoding=3,
                    mime=mime_type,
                    type=3,  # 封面图片
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
        print(f"!添加MP3元数据失败: {e}")
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
                        print(f"!下载内容过小: {len(content)} bytes")
                else:
                    print(f"!下载失败，状态码: {resp.status}")
                return None
    except Exception as e:
        print(f"!下载文件时出错: {e}")
        return None


async def load_and_refresh_credential() -> Credential | None:
    """加载本地凭证，如果过期则自动刷新"""
    if not CREDENTIAL_FILE.exists():
        print("本地无凭证文件，仅能下载免费歌曲")
        return None

    try:
        with CREDENTIAL_FILE.open("rb") as f:
            cred: Credential = pickle.load(f)

        # 检查是否过期
        is_expired = await check_expired(cred)

        if is_expired:
            print("本地凭证已过期，尝试自动刷新...")

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
                    print("将以未登录方式下载")
                    return None
            else:
                print("凭证不支持刷新，将以未登录方式下载")
                return None
        else:
            print(f"使用本地凭证登录成功!")
            return cred

    except Exception as e:
        print(f"加载凭证失败: {e}，将以未登录方式下载")
        return None


async def search_song() -> dict:
    """搜索歌曲并让用户选择要下载的那一首"""
    keyword = ""
    while not keyword:
        keyword = input("请输入要搜索的歌曲: ").strip()
        if not keyword:
            print("歌曲名不能为空，请重新输入。")

    # 搜索前 5 条结果
    results = await search.search_by_type(keyword, num=5)
    if not results:
        raise ValueError("未找到歌曲")

    print("\n搜索结果如下：")
    for idx, song in enumerate(results, start=1):
        name = song["title"]
        singers = ", ".join([s["name"] for s in song["singer"]])
        # 判断是否为 VIP 歌曲
        vip_flag = song.get("pay", {}).get("pay_play", 0) != 0
        vip_label = " [VIP]" if vip_flag else ""
        print(f"{idx}. {name} - {singers}{vip_label}")

    # 用户选择要下载的歌曲
    choice = 0
    while not (1 <= choice <= len(results)):
        try:
            choice = int(input(f"请输入要下载的序号 (1-{len(results)}): "))
        except ValueError:
            print("请输入有效数字。")

    song_info = results[choice - 1]
    vip_flag = song_info.get("pay", {}).get("pay_play", 0) != 0
    print(f"\n你选择了: {song_info['title']} - {song_info['singer'][0]['name']}{' [VIP]' if vip_flag else ''}")

    return song_info


async def download_song_with_fallback(song_info: dict, credential: Credential | None, prefer_flac: bool):
    """下载歌曲，根据音质偏好进行降级下载"""
    vip = song_info.get('pay', {}).get('pay_play', 0) != 0
    if vip and not credential:
        print("这首歌是VIP歌曲，需要登录才能下载高音质版本")

    mid = song_info['mid']
    song_name = song_info['title']
    singer_name = song_info['singer'][0]['name']
    album_info = song_info.get('album', {})
    album_name = album_info.get('name', '')
    album_mid = album_info.get('mid', '')

    # 根据音质偏好设置下载策略
    if prefer_flac:
        # FLAC优先策略：FLAC -> MP3_320 -> MP3_128
        quality_order = [
            (SongFileType.FLAC, "FLAC"),
            (SongFileType.MP3_320, "320kbps"),
            (SongFileType.MP3_128, "128kbps")
        ]
        print("使用高品质音质策略: FLAC -> MP3_320 -> MP3_128")
    else:
        # MP3优先策略：MP3_320 -> MP3_128
        quality_order = [
            (SongFileType.MP3_320, "320kbps"),
            (SongFileType.MP3_128, "128kbps")
        ]
        print("使用标准音质策略: MP3_320 -> MP3_128")

    # 清理文件名中的非法字符
    def sanitize_filename(filename: str) -> str:
        illegal_chars = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
        for char in illegal_chars:
            filename = filename.replace(char, '_')
        return filename

    safe_filename = sanitize_filename(f"{song_name}-{singer_name}")

    # 尝试不同音质
    downloaded_file_type = None
    for file_type, quality_name in quality_order:
        filepath = MUSIC_DIR / f"{safe_filename}{file_type.e}"

        # 如果文件已存在，跳过下载
        if filepath.exists():
            print(f"文件已存在，跳过: {safe_filename}{file_type.e} ({quality_name})")
            downloaded_file_type = file_type
            return True

        print(f">尝试下载 {quality_name}: {safe_filename}{file_type.e}{' [VIP]' if vip else ''}")

        # 获取歌曲URL
        urls = await get_song_urls([mid], file_type=file_type, credential=credential)
        url = urls.get(mid)

        if not url:
            print(f"!无法获取歌曲URL ({quality_name})")
            continue

        if isinstance(url, list):
            url = url[0]

        # 下载歌曲
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        # 检查文件是否有效
                        if len(content) > 1024:
                            with open(filepath, "wb") as f:
                                f.write(content)
                            print(f"-->下载成功 ({quality_name}): {filepath}")
                            print(f"下载链接: {url}")
                            downloaded_file_type = file_type

                            # 为所有文件自动添加元数据
                            try:
                                # 获取封面URL
                                cover_url = None
                                if album_mid:
                                    cover_url = get_cover(album_mid, cover_size)  # 使用800px大小的封面

                                # 获取歌词
                                lyrics_data = None
                                try:
                                    lyrics_data = await get_lyric(mid)
                                except Exception:
                                    pass  # 静默失败

                                # 根据文件类型添加元数据
                                if cover_url or lyrics_data:
                                    if downloaded_file_type == SongFileType.FLAC and filepath.suffix.lower() == '.flac':
                                        await add_metadata_to_flac(
                                            filepath,
                                            song_info,
                                            cover_url,
                                            lyrics_data
                                        )
                                    elif filepath.suffix.lower() in ['.mp3', '.m4a']:
                                        await add_metadata_to_mp3(
                                            filepath,
                                            song_info,
                                            cover_url,
                                            lyrics_data
                                        )

                            except Exception:
                                pass  # 静默处理元数据添加失败

                            return True
                        else:
                            print(f"!{quality_name}文件过小，可能下载失败")
                    else:
                        print(f"!{quality_name}下载失败，HTTP状态码: {resp.status}")
            except Exception as e:
                print(f"!{quality_name}下载时发生错误: {e}")

    # 所有音质都尝试失败
    print("所有音质下载失败")
    return False


async def main():
    print("QQ音乐单曲下载器")
    print("版本号:v2.0.4")
    print("-" * 50)
    # 尝试加载本地凭证（包含自动刷新功能）
    credential = await load_and_refresh_credential()

    # 询问音质偏好
    print("-" * 50)
    flac_choice = input("你希望更高音质吗？(y/n): ").strip().lower()

    if flac_choice == 'y':
        prefer_flac = True
        print("已选择高品质音质 (FLAC优先)")
    else:
        prefer_flac = False
        print("已选择标准音质 (MP3_320优先)")

    while True:
        try:
            song_info = await search_song()
            await download_song_with_fallback(song_info, credential, prefer_flac)
        except ValueError as e:
            print(f"错误: {e}")
        except Exception as e:
            print(f"发生未知错误: {e}")
        print("-" * 20)  # 添加分隔符


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序被用户中断，正在退出。")