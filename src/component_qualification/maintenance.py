"""对既有数据库执行隔离扫描与处置的命令行维护工具。"""

from __future__ import annotations

import argparse
import json

from .service import ComponentService


def scan(database: str, lot_id: str | None, admin_password: str = "component-admin") -> dict:
    service = ComponentService(database)
    service.bootstrap_admin(password=admin_password)
    token = service.auth.login("admin", admin_password)
    result = service.scan_quarantine(token, lot_id)
    return result


def list_quarantine(database: str, lot_id: str | None, admin_password: str = "component-admin") -> list[dict]:
    service = ComponentService(database)
    service.bootstrap_admin(password=admin_password)
    token = service.auth.login("admin", admin_password)
    return service.list_quarantine(token, lot_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="国产部件测量隔离维护工具")
    parser.add_argument("--database", required=True, help="SQLite 数据库文件")
    parser.add_argument("--lot", dest="lot_id", default=None, help="仅扫描指定批次")
    parser.add_argument("--list", action="store_true", help="只列出现有隔离记录")
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps({"quarantined": list_quarantine(args.database, args.lot_id)},
                         ensure_ascii=False, allow_nan=False))
    else:
        print(json.dumps(scan(args.database, args.lot_id), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
