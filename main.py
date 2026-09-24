import platform
import sys


def main() -> None:
    system = platform.system()

    if system == "Darwin":
        from Mac.app import run
        run()
    else:
        print(f"No UI implemented yet for {system}. Only macOS is supported right now.")
        sys.exit(1)


if __name__ == "__main__":
    main()
