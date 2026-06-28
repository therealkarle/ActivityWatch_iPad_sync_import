from __future__ import annotations

import sys

from .activitywatch_client import ActivityWatchError
from .config import ConfigError, ensure_config_example, load_config, project_root
from .filesystem import BackupNotFoundError
from .importer import run_import
from .screen_time import ScreenTimeError


def main() -> int:
    ensure_config_example(project_root())

    try:
        config = load_config(project_root())
        imported = run_import(config, verbose=True)
    except ConfigError as exc:
        print(exc)
        return 1
    except BackupNotFoundError as exc:
        print(exc)
        return 1
    except ScreenTimeError as exc:
        print(exc)
        return 1
    except ActivityWatchError as exc:
        print(exc)
        return 1
    except KeyboardInterrupt:
        print("Aborted while waiting for ActivityWatch.")
        return 130

    print(f"{imported} events imported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
