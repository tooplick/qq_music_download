#!/usr/bin/env python3
"""
QQ音乐凭证管理工具
功能：检查凭证状态、手动刷新凭证
"""

import pickle
import asyncio
from pathlib import Path
from typing import Optional

from qqmusic_api.login import Credential, check_expired

# 配置
CREDENTIAL_FILE = Path("qqmusic_cred.pkl")


class CredentialManager:
    """凭证管理器"""

    def __init__(self, credential_file: Path = CREDENTIAL_FILE):
        self.credential_file = credential_file
        self.credential = None

    def load_credential(self) -> Optional[Credential]:
        """加载本地凭证"""
        if not self.credential_file.exists():
            print("未找到凭证文件，请先运行登录程序")
            return None

        try:
            with self.credential_file.open("rb") as f:
                cred = pickle.load(f)
            self.credential = cred
            return cred
        except Exception as e:
            print(f"加载凭证失败: {e}")
            return None

    def save_credential(self) -> bool:
        """保存凭证到文件"""
        if not self.credential:
            print("没有可保存的凭证")
            return False

        try:
            with self.credential_file.open("wb") as f:
                pickle.dump(self.credential, f)
            print("凭证已保存")
            return True
        except Exception as e:
            print(f"保存凭证失败: {e}")
            return False

    async def check_status(self) -> bool:
        """检查凭证状态"""
        if not self.load_credential():
            return False

        print("\n凭证状态检查:")
        print("-" * 30)

        # 检查是否过期
        is_expired = await check_expired(self.credential)
        print(f"是否过期: {'是' if is_expired else '否'}")

        # 检查是否可以刷新 - 使用await
        can_refresh = await self.credential.can_refresh()
        print(f"可刷新: {'是' if can_refresh else '否'}")

        if hasattr(self.credential, 'musicid'):
            print(f"用户ID: {self.credential.musicid}")

        return not is_expired

    async def manual_refresh(self) -> bool:
        """手动刷新凭证（交互式）"""
        if not self.load_credential():
            return False

        print("\n手动刷新凭证")
        print("-" * 30)

        # 显示当前状态
        is_expired = await check_expired(self.credential)
        can_refresh = await self.credential.can_refresh()  # 使用await

        print(f"当前状态: {'已过期' if is_expired else '有效'}")
        print(f"可刷新: {'是' if can_refresh else '否'}")

        if not can_refresh:
            print("此凭证不支持刷新，无法继续")
            return False

        # 确认是否刷新
        confirm = input("\n确定要刷新凭证吗,之前的凭证会失效？(y/N): ").strip().lower()
        if confirm != 'y':
            print("取消刷新")
            return False

        try:
            print("正在刷新凭证...")
            await self.credential.refresh()
            self.save_credential()
            print("凭证刷新成功")

            # 保存刷新后的凭证
            if self.save_credential():
                print("刷新完成！凭证已更新并保存")
                return True
            else:
                print("凭证刷新成功但保存失败")
                return False

        except Exception as e:
            print(f"刷新失败: {e}")
            return False

    def show_credential_info(self):
        """显示凭证信息"""
        if not self.load_credential():
            return

        print("\n凭证信息:")
        print("-" * 30)

        # 显示凭证的基本信息
        cred_dict = self.credential.__dict__
        for key, value in cred_dict.items():
            if key.lower() in ['token', 'refresh_token', 'cookie']:
                # 敏感信息，只显示部分
                if value and len(str(value)) > 10:
                    display_value = f"{str(value)[:10]}..."
                else:
                    display_value = str(value)
            else:
                display_value = str(value)

            print(f"{key}: {display_value}")


async def main():
    """主函数"""
    manager = CredentialManager()

    print("QQ音乐凭证管理工具")
    print("=" * 40)

    while True:
        print("\n请选择操作:")
        print("1. 检查凭证状态")
        print("2. 手动刷新凭证")
        print("3. 显示凭证信息")
        print("4. 退出")

        choice = input("\n请输入选项 (1-4): ").strip()

        if choice == '1':
            await manager.check_status()
            input("\n按回车键继续...")

        elif choice == '2':
            success = await manager.manual_refresh()
            if success:
                print("手动刷新完成")
            else:
                print("手动刷新失败")
            input("\n按回车键继续...")

        elif choice == '3':
            manager.show_credential_info()
            input("\n按回车键继续...")

        elif choice == '4':
            print("再见！")
            break

        else:
            print("无效选择，请重新输入")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n用户中断，程序退出")