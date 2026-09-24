import signal
from pathlib import Path

from AppKit import NSApplication, NSApplicationActivationPolicyRegular, NSImage
from PyObjCTools import AppHelper

from core.manager import ProxyManager
from .glass_window import GlassWindowController

ICON_PATH = Path(__file__).resolve().parent / "Resources" / "icon.icns"


def run() -> None:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    if ICON_PATH.exists():
        icon = NSImage.alloc().initWithContentsOfFile_(str(ICON_PATH))
        if icon is not None:
            app.setApplicationIconImage_(icon)

    manager = ProxyManager()
    controller = GlassWindowController.alloc().initWithManager_(manager)
    app.setDelegate_(controller)

    signal.signal(signal.SIGTERM, lambda signum, frame: app.terminate_(None))

    controller.show()

    AppHelper.runEventLoop()
