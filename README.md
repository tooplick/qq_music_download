# QQ音乐下载器

一个基于Python的QQ音乐下载工具，支持单曲搜索下载和歌单批量下载。

## 功能特点

- **单曲下载** - 支持搜索并下载单首歌曲
- **歌单下载** - 支持下载公开歌单
- **登录支持** - 支持QQ/微信登录，可下载VIP歌曲
- **多音质选择** - 支持FLAC、MP3_320、MP3_128等音质
- **统一凭证管理** - 登录、检查、刷新一站式管理

## 安装要求

### 系统要求
- Python 3.10+
- Windows/macOS/Linux

### Clone 到本地
```python
git clone --depth=1 https://github.com/tooplick/qq_music_download
cd qq_music_download
```
### 安装依赖
```python
pip install -r requirements.txt
```
**或者使用 `pyproject.toml` 安装依赖**
```python
pip install .
```
## 使用方法

### 1. 登录与凭证管理（推荐）
```python
python credential.py
```

### 2. 单曲下载
```python
python song.py
```

### 3. 歌单下载
```python
python songlist.py
```
### 4. 如果运行报错
**请运行**：
   - `pip install qqmusic-api-python flask aiohttp mutagen`
   - `sudo apt update && sudo apt install libzbar0 libzbar-dev`

## 配置参数说明
- `COVER_SIZE = 800`: 封面图片尺寸选项,支持[150, 300, 500, 800]
- `DOWNLOAD_TIMEOUT = 30`: 网络请求超时时间
- `CREDENTIAL_FILE = Path("qqmusic_cred.pkl")`: 凭证文件存储位置
- `MUSIC_DIR = Path("./music")`: 音乐文件保存目录
- `MIN_FILE_SIZE = 1024`: 文件完整性检查阈值
- `SEARCH_RESULTS_COUNT = 5`: 搜索结果数量（单曲专用）
- `SEARCH_RESULTS_COUNT = 5`: 并发下载数量（歌单专用）
- `FOLDER_NAME = "{songlist_name}"` = 歌单文件夹名称格式

## 文件说明

- `song.py` - 单曲搜索下载
- `songlist.py` - 歌单下载
- `credential.py` - 登录与凭证管理（合并了原signin.py和credential.py）
- `requirements.txt` - 项目依赖
- `qqmusic_cred.pkl` - 登录凭证（自动生成）
- `credential.spec` - 打包 credential.py 的配置文件
- `windows打包文件` - 见Releases

## 音质说明

### 高品质模式 (FLAC优先)
- FLAC → MP3_320 → MP3_128
- 优先下载无损音质

### 标准模式 (MP3优先)
- MP3_320 → MP3_128
- 优先下载高品质MP3

## 更新日志
### v2.1.1
- 终端显示登录二维码
- 解决封面添加失败问题
- 自定义歌单文件夹名称格式

### v2.1.0
- 重构优化代码逻辑
- 提高性能

### v2.0.4
- 自动添加歌词封面(800px)
- 一键下载所有歌单

### v2.0.3
- 合并登录与凭证管理功能
- 添加自动凭证刷新机制

### v2.0.2
- 基础单曲和歌单下载功能
- 多音质支持
- 登录功能

### v2.0.1
- 初始版本

## 作者信息

- **作者**：GeQian
- **GitHub**：[https://github.com/tooplick](https://github.com/tooplick)

---

## 免责声明
- 本代码遵循 [GPL-3.0 License](https://github.com/tooplick/qq_music_download/blob/main/LICENSE) 协议
   - 允许**开源/免费使用和引用/修改/衍生代码的开源/免费使用**
   - 不允许**修改和衍生的代码作为闭源的商业软件发布和销售**
   - 禁止**使用本代码盈利**
- 以此代码为基础的程序**必须**同样遵守 [GPL-3.0 License](https://github.com/tooplick/qq_music_download/blob/main/LICENSE) 协议
- 本代码仅用于**学习讨论**，禁止**用于盈利**,下载的音乐请于**24小时内删除**,支持**正版音乐**
- 他人或组织使用本代码进行的任何**违法行为**与本人无关