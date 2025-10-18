import asyncio
import pickle
from pathlib import Path
from qqmusic_api import search
from qqmusic_api.song import get_song_urls, SongFileType
from qqmusic_api.login import Credential, check_expired
from qqmusic_api.user import get_vip_info
import aiohttp
import os

CREDENTIAL_FILE = Path("qqmusic_cred.pkl")
MUSIC_DIR = Path("./music")
MUSIC_DIR.mkdir(exist_ok=True)


async def load_credential() -> Credential | None:
    """加载本地凭证，如果不存在或过期返回 None"""
    if not CREDENTIAL_FILE.exists():
        return None
    try:
        with CREDENTIAL_FILE.open("rb") as f:
            cred: Credential = pickle.load(f)
        if await check_expired(cred):
            print("本地凭证已过期，将以未登录方式下载")
            return None
        print(f"使用本地凭证登录成功! ")
        return cred
    except Exception:
        print("加载凭证失败，将以未登录方式下载")
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
    for file_type, quality_name in quality_order:
        filepath = MUSIC_DIR / f"{safe_filename}{file_type.e}"

        # 如果文件已存在，跳过下载
        if filepath.exists():
            print(f"文件已存在，跳过: {safe_filename}{file_type.e} ({quality_name})")
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
    # 尝试加载本地凭证
    credential = await load_credential()

    # 询问音质偏好
    print("\n" + "=" * 50)
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