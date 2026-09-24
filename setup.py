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
    "packages": [
        "core",
        "mcp_server",
        "mcp",
        "uvicorn",
        "starlette",
        "sse_starlette",
        "anyio",
        "pydantic",
        "pydantic_core",
        "keyring",
        "cryptography",
    ],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
