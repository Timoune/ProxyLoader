from setuptools import setup

APP = ["main.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "Mac/Resources/icon.icns",
    "plist": {
        "CFBundleName": "proxyloader",
        "CFBundleDisplayName": "proxyloader",
        "CFBundleIconFile": "icon.icns",
        "LSUIElement": False,
    },
    "packages": ["core"],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
