"""Create a deliberately broken Fabric 1.20.1 instance so you can try Modpack Doctor safely.

    python tools/make_demo_pack.py ./demo-pack
    python -m modpack_doctor gui ./demo-pack
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
import fakepack  # noqa: E402


def main() -> int:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-pack").resolve()
    if dest.exists() and any(dest.iterdir()):
        print(f"{dest} already exists and is not empty; choose another folder.", file=sys.stderr)
        return 1
    fakepack.make_fabric_instance(dest)
    print(f"Demo instance created in {dest}\nOpen it with:  python -m modpack_doctor gui \"{dest}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
