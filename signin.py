# login.py
import asyncio
import pickle
import os
from pathlib import Path
from qqmusic_api.login import get_qrcode, check_qrcode, QRLoginType, Credential, QRCodeLoginEvents

CREDENTIAL_FILE = Path("qqmusic_cred.pkl")

async def qr_login(qr_type: QRLoginType) -> Credential | None:
    """二维码登录并保存凭证"""
    qr = await get_qrcode(qr_type)
    qr_path = qr.save()
    print(f"请扫描二维码登录，二维码已保存至: {qr_path} ...")

    credential = None
    while True:
        event, credential = await check_qrcode(qr)
        print(f"二维码状态: {event.name}")
        if event == QRCodeLoginEvents.DONE:
            print(f"登录成功! {credential}")
            # 保存凭证
            with CREDENTIAL_FILE.open("wb") as f:
                pickle.dump(credential, f)
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

async def main():
    print("请选择登录方式:")
    print("1. QQ 二维码")
    print("2. 微信二维码")
    choice = input("请输入选项 (1/2): ").strip()

    if choice == "1":
        await qr_login(QRLoginType.QQ)
    elif choice == "2":
        await qr_login(QRLoginType.WX)
    else:
        print("跳过登录")

if __name__ == "__main__":
    asyncio.run(main())
