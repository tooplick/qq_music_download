#!/usr/bin/env python3
"""
QQ音乐凭证管理工具
功能：登录、检查凭证状态、手动刷新凭证、凭证管理
"""

import asyncio
import pickle
import os
from pathlib import Path
from typing import Optional
from qqmusic_api.login import get_qrcode, check_qrcode, QRLoginType, Credential, QRCodeLoginEvents, check_expired

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

    async def qr_login(self, qr_type: QRLoginType) -> Credential | None:
        """二维码登录并保存凭证"""
        qr = await get_qrcode(qr_type)
        qr_path = qr.save()
        print(f"请扫描二维码登录，二维码已保存至: {qr_path} ...")

        credential = None
        while True:
            event, credential = await check_qrcode(qr)
            print(f"二维码状态: {event.name}")
            if event == QRCodeLoginEvents.DONE:
                print(f"登录成功! 用户ID: {credential.musicid if hasattr(credential, 'musicid') else '未知'}")
                # 保存凭证
                self.credential = credential
                self.save_credential()
                # 删除二维码图片
                if os.path.exists(qr_path):
                    os.remove(qr_path)
                return credential
            elif event == QRCodeLoginEvents.TIMEOUT:
                print("二维码过期，请重新运行程序")
                return None
            elif event == QRCodeLoginEvents.REFUSE:
                print("拒绝登录，请重新扫码")
                return None
            await asyncio.sleep(2)

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

    # 检查是否存在凭证
    if not manager.load_credential():
        print("未找到凭证文件，需要先登录")
        print("\n请选择登录方式:")
        print("1. QQ 二维码")
        print("2. 微信二维码")
        print("3. 取消")
        choice = input("请输入选项 (1-3): ").strip()

        if choice == "1":
            await manager.qr_login(QRLoginType.QQ)
        elif choice == "2":
            await manager.qr_login(QRLoginType.WX)
        elif choice == "3":
            print("取消登录，退出程序")
            return
        else:
            print("无效选择，退出程序")
            return

    # 凭证管理菜单
    while True:
        print("\n请选择操作:")
        print("1. 检查凭证状态")
        print("2. 手动刷新凭证")
        print("3. 显示凭证信息")
        print("4. 重新登录")
        print("5. 退出")

        choice = input("\n请输入选项 (1-5): ").strip()

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
            print("\n请选择登录方式:")
            print("1. QQ 二维码")
            print("2. 微信二维码")
            print("3. 取消")
            login_choice = input("请输入选项 (1-3): ").strip()

            if login_choice == "1":
                await manager.qr_login(QRLoginType.QQ)
            elif login_choice == "2":
                await manager.qr_login(QRLoginType.WX)
            elif login_choice == "3":
                print("取消重新登录")
            else:
                print("无效选择")

        elif choice == '5':
            print("再见！")
            break

        else:
            print("无效选择，请重新输入")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n用户中断，程序退出")