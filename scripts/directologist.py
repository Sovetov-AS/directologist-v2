#!/usr/bin/env python3
"""Единая точка входа; требуется выбранный проект и Python 3.12+."""

import sys

if sys.version_info < (3, 12):
    print('Директолог требует Python 3.12+. См. README.md: системный python3 может быть старее.', file=sys.stderr)
    raise SystemExit(2)

from directologist.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
