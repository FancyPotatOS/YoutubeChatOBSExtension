"""SQL Server-backed stream user inventory helpers.

Connection configuration is read from environment variables or ``secrets.json``.
The simplest ``secrets.json`` shape is:

```
{
  "inventory_database": {
    "server": ".\\SQLEXPRESS",
    "database": "StreamInventory",
    "username": "stream_bot",
    "password": "change-me",
    "driver": "ODBC Driver 18 for SQL Server"
  }
}
```

You can also provide ``connection_string`` directly. If a connection string
contains ``Server=...`` but not ``DRIVER=...`` or ``DSN=...``, the configured
driver is added automatically.

The default table names are ``users``, ``items``, and ``inventory``. Override
them with STREAM_INVENTORY_USERS_TABLE, STREAM_INVENTORY_ITEMS_TABLE, and
STREAM_INVENTORY_TABLE, or with ``tables`` in ``secrets.json``.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import uuid


DEFAULT_WALLET = 0
USER_ID_NAMESPACE = uuid.UUID("f426846b-2f85-4a04-89dd-449c6978596e")
SECRETS_PATH = Path(__file__).with_name("secrets.json")
SECRET_SECTIONS = ("inventory_database", "stream_inventory", "inventory")
DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"


class InventoryError(RuntimeError):
    """Base error for stream inventory failures."""


class InventoryConfigError(InventoryError):
    """Raised when the database connection is not configured."""


class InventoryInputError(InventoryError):
    """Raised when chat data does not include enough inventory information."""


def register_user(data, browser=None):
    """Ensure the chat author exists in the users table."""
    del browser

    name = _extract_user_name(data)
    requested_user_id = _extract_optional_uuid(data, "user_id", "userId")

    connection = _connect()
    try:
        cursor = connection.cursor()
        user, created = _get_or_create_user(cursor, name, requested_user_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    status = "Registered" if created else "Already registered"
    print(f"{status}: {user['name']} ({user['user_id']}) wallet={user['wallet']}")
    return {**user, "created": created}


def auction_item(data, browser=None):
    """Remove one matching item from a user's inventory for a future auction."""
    del browser

    user_name = _extract_user_name(data)
    item_selector = _extract_item_selector(data)

    connection = _connect()
    try:
        cursor = connection.cursor()
        user, _created = _get_or_create_user(cursor, user_name)
        item = _resolve_item(cursor, item_selector)
        inventory_item = _get_inventory_item(cursor, user["user_id"], item["item_id"])
        if inventory_item is None:
            raise InventoryInputError(
                f"{user['name']} does not have {item['name']} in inventory"
            )

        _delete_inventory_row(cursor, inventory_item["inventory_id"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(f"Queued for auction: {user['name']} removed {item['name']}")
    return {"user": user, "item": item, "inventory_id": inventory_item["inventory_id"]}


def sell_item(data, browser=None):
    """Sell one matching inventory item and add its base value to the wallet."""
    del browser

    user_name = _extract_user_name(data)
    item_selector = _extract_item_selector(data)

    connection = _connect()
    try:
        cursor = connection.cursor()
        user, _created = _get_or_create_user(cursor, user_name)
        item = _resolve_item(cursor, item_selector)
        inventory_item = _get_inventory_item(cursor, user["user_id"], item["item_id"])
        if inventory_item is None:
            raise InventoryInputError(
                f"{user['name']} does not have {item['name']} in inventory"
            )

        _delete_inventory_row(cursor, inventory_item["inventory_id"])
        new_wallet = _add_to_wallet(cursor, user["user_id"], item["base_value"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    user["wallet"] = new_wallet
    print(
        f"Sold {item['name']} for {item['base_value']}; "
        f"{user['name']} wallet={new_wallet}"
    )
    return {
        "user": user,
        "item": item,
        "inventory_id": inventory_item["inventory_id"],
        "wallet_added": item["base_value"],
    }


# Couple helper methods
def add_item_to_inventory(user_id, item_id):
    """Add one item row to a user's inventory and return the inventory id."""
    user_id = _uuid_text(user_id, "user_id")
    item_id = _uuid_text(item_id, "item_id")
    inventory_id = str(uuid.uuid4())

    connection = _connect()
    try:
        cursor = connection.cursor()
        cursor.execute(
            f"""
INSERT INTO {_table('inventory')} (inventory_id, user_id, item_id)
VALUES (?, ?, ?)
""",
            inventory_id,
            user_id,
            item_id,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return inventory_id


def remove_item_from_inventory(user_id, item_id):
    """Remove one matching item row from a user's inventory."""
    user_id = _uuid_text(user_id, "user_id")
    item_id = _uuid_text(item_id, "item_id")

    connection = _connect()
    try:
        cursor = connection.cursor()
        inventory_item = _get_inventory_item(cursor, user_id, item_id)
        if inventory_item is None:
            connection.rollback()
            return False

        _delete_inventory_row(cursor, inventory_item["inventory_id"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return True


def get_user_inventory(user_id):
    """Get the user's inventory from the database as a list of item ids."""
    user_id = _uuid_text(user_id, "user_id")

    connection = _connect()
    try:
        cursor = connection.cursor()
        rows = cursor.execute(
            f"""
SELECT item_id
FROM {_table('inventory')}
WHERE user_id = ?
ORDER BY inventory_id
""",
            user_id,
        ).fetchall()
    finally:
        connection.close()

    return [str(row.item_id) for row in rows]


def _connect():
    try:
        import pyodbc
    except ModuleNotFoundError as error:
        raise InventoryConfigError(
            "pyodbc is required for SQL Server inventory access. "
            "Install it with: pip install pyodbc"
        ) from error

    try:
        return pyodbc.connect(_connection_string())
    except pyodbc.Error as error:
        raise InventoryConfigError(
            f"Could not connect to inventory database: {_format_connection_error(pyodbc, error)}"
        ) from error


def _connection_string():
    secrets = _load_inventory_secrets()
    connection_string = _first_config_value(
        secrets,
        ("STREAM_INVENTORY_CONNECTION_STRING",),
        ("connection_string", "connectionString"),
    )
    if connection_string:
        return _normalize_connection_string(str(connection_string), secrets)

    server = _first_config_value(
        secrets,
        ("STREAM_INVENTORY_SERVER",),
        ("server", "host", "data_source", "dataSource"),
    )
    database = _first_config_value(
        secrets,
        ("STREAM_INVENTORY_DATABASE",),
        ("database", "database_name", "databaseName"),
    )
    if not server or not database:
        raise InventoryConfigError(
            "Set inventory_database.connection_string in secrets.json, or set "
            "inventory_database.server and inventory_database.database."
        )

    driver = _configured_driver(secrets)
    parts = [
        _driver_part(driver),
        _connection_part("SERVER", server),
        _connection_part("DATABASE", database),
    ]

    uid = _first_config_value(
        secrets,
        ("STREAM_INVENTORY_UID", "STREAM_INVENTORY_USERNAME"),
        ("uid", "user", "username"),
    )
    pwd = _first_config_value(
        secrets,
        ("STREAM_INVENTORY_PWD", "STREAM_INVENTORY_PASSWORD"),
        ("pwd", "password"),
        allow_empty=True,
    )
    if uid:
        parts.extend([_connection_part("UID", uid), _connection_part("PWD", pwd)])
    else:
        trusted = _first_config_value(
            secrets,
            ("STREAM_INVENTORY_TRUSTED_CONNECTION",),
            ("trusted_connection", "trustedConnection"),
        )
        parts.append(f"Trusted_Connection={_yes_no(trusted, default='yes')}")

    encrypt = _first_config_value(
        secrets,
        ("STREAM_INVENTORY_ENCRYPT",),
        ("encrypt",),
    )
    if encrypt:
        parts.append(f"Encrypt={_yes_no(encrypt)}")

    trust_cert = _first_config_value(
        secrets,
        ("STREAM_INVENTORY_TRUST_SERVER_CERTIFICATE",),
        ("trust_server_certificate", "trustServerCertificate"),
    )
    if trust_cert:
        parts.append(f"TrustServerCertificate={_yes_no(trust_cert)}")
    else:
        parts.append("TrustServerCertificate=yes")

    return ";".join(parts)


def _table(name):
    secrets = _load_inventory_secrets()
    tables = secrets.get("tables") if isinstance(secrets.get("tables"), Mapping) else {}
    table_names = {
        "users": _first_config_value(
            secrets,
            ("STREAM_INVENTORY_USERS_TABLE",),
            ("users_table", "usersTable"),
        )
        or tables.get("users")
        or "users",
        "items": _first_config_value(
            secrets,
            ("STREAM_INVENTORY_ITEMS_TABLE",),
            ("items_table", "itemsTable"),
        )
        or tables.get("items")
        or "items",
        "inventory": _first_config_value(
            secrets,
            ("STREAM_INVENTORY_TABLE",),
            ("inventory_table", "inventoryTable"),
        )
        or tables.get("inventory")
        or "inventory",
    }
    return _quote_table_name(table_names[name])


def _load_inventory_secrets():
    if not SECRETS_PATH.is_file():
        return {}

    try:
        with SECRETS_PATH.open("r", encoding="utf-8") as secrets_file:
            secrets = json.load(secrets_file)
    except OSError as error:
        raise InventoryConfigError(f"Could not read {SECRETS_PATH.name}: {error}") from error
    except json.JSONDecodeError as error:
        raise InventoryConfigError(f"{SECRETS_PATH.name} is not valid JSON: {error}") from error

    if not isinstance(secrets, Mapping):
        raise InventoryConfigError(f"{SECRETS_PATH.name} must contain a JSON object")

    for section_name in SECRET_SECTIONS:
        section = secrets.get(section_name)
        if isinstance(section, Mapping):
            return dict(section)

    return dict(secrets)


def _first_config_value(secrets, env_names, secret_names, *, allow_empty=False):
    for env_name in env_names:
        if env_name in os.environ and (allow_empty or os.environ[env_name].strip()):
            return os.environ[env_name]

    for secret_name in secret_names:
        if secret_name not in secrets:
            continue

        value = secrets[secret_name]
        if value is None:
            continue
        if isinstance(value, str):
            if allow_empty or value.strip():
                return value.strip()
            continue
        return value

    return ""


def _configured_driver(secrets):
    return str(
        _first_config_value(
            secrets,
            ("STREAM_INVENTORY_DRIVER",),
            ("driver", "odbc_driver", "odbcDriver"),
        )
        or DEFAULT_DRIVER
    ).strip()


def _driver_part(driver):
    driver = str(driver).strip().strip("{}")
    return f"DRIVER={{{driver}}}"


def _connection_part(key, value):
    return f"{key}={_connection_value(value)}"


def _connection_value(value):
    text = str(value)
    if text != text.strip() or any(character in text for character in ";{}"):
        return "{" + text.replace("}", "}}") + "}"
    return text


def _normalize_connection_string(connection_string, secrets):
    connection_string = connection_string.strip().rstrip(";")
    lowered = connection_string.lower()
    parts = []

    if "driver=" not in lowered and "dsn=" not in lowered:
        parts.append(_driver_part(_configured_driver(secrets)))

    parts.append(connection_string)

    if "trustservercertificate=" not in lowered and "dsn=" not in lowered:
        trust_cert = _first_config_value(
            secrets,
            ("STREAM_INVENTORY_TRUST_SERVER_CERTIFICATE",),
            ("trust_server_certificate", "trustServerCertificate"),
        )
        parts.append(f"TrustServerCertificate={_yes_no(trust_cert, default='yes')}")

    return ";".join(part for part in parts if part)


def _yes_no(value, *, default=""):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return "yes" if value else "no"

    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return "yes"
    if text in {"0", "false", "no", "n", "off"}:
        return "no"
    return str(value).strip()


def _format_connection_error(pyodbc, error):
    message = str(error)
    if "IM002" not in message:
        return message

    secrets = _load_inventory_secrets()
    drivers = ", ".join(pyodbc.drivers()) or "none detected"
    return (
        f"{message}. SQL Server could not find a DSN or ODBC driver. "
        f"Configured driver: {_configured_driver(secrets)!r}. "
        f"Installed ODBC drivers: {drivers}. "
        f"Set inventory_database.driver in {SECRETS_PATH.name} to one of those names."
    )


def _quote_table_name(table_name):
    parts = [part.strip().strip("[]") for part in str(table_name).split(".")]
    if any(not part for part in parts):
        raise InventoryConfigError(f"Invalid table name: {table_name!r}")

    return ".".join(f"[{part.replace(']', ']]')}]" for part in parts)


def _extract_user_name(data):
    if isinstance(data, str):
        name = data
    else:
        name = (
            data.get("authorName")
            or data.get("name")
            or data.get("author")
            or data.get("user")
            or ""
        )

    name = str(name).strip()
    if not name:
        raise InventoryInputError("Chat data does not include a user name")
    return name


def _extract_optional_uuid(data, *keys):
    if not isinstance(data, dict):
        return None

    for key in keys:
        value = data.get(key)
        if value:
            return _uuid_text(value, key)
    return None


def _extract_item_selector(data):
    if not isinstance(data, dict):
        raise InventoryInputError("Item data must be a chat payload dictionary")

    for key in ("item_id", "itemId", "item_name", "itemName", "item"):
        value = data.get(key)
        if value:
            return str(value).strip()

    argument = _message_argument(data)
    if argument:
        return argument

    raise InventoryInputError("Provide an item id or item name after the command")


def _message_argument(data):
    message = data.get("message") or data.get("rawText") or ""
    message = str(message).strip()
    if not message:
        return ""

    if message.startswith("!"):
        parts = message.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
    return message


def _uuid_text(value, field_name):
    if isinstance(value, uuid.UUID):
        return str(value)

    try:
        return str(uuid.UUID(str(value).strip()))
    except (TypeError, ValueError) as error:
        raise InventoryInputError(f"{field_name} must be a UUID: {value!r}") from error


def _stable_user_id(name):
    return str(uuid.uuid5(USER_ID_NAMESPACE, name.casefold()))


def _get_or_create_user(cursor, name, user_id=None):
    row = cursor.execute(
        f"""
SELECT TOP (1) user_id, name, wallet
FROM {_table('users')}
WHERE name = ?
""",
        name,
    ).fetchone()
    if row is not None:
        return _user_from_row(row), False

    user_id = user_id or _stable_user_id(name)
    wallet = DEFAULT_WALLET
    cursor.execute(
        f"""
INSERT INTO {_table('users')} (user_id, name, wallet)
VALUES (?, ?, ?)
""",
        user_id,
        name,
        wallet,
    )
    return {"user_id": user_id, "name": name, "wallet": wallet}, True


def _resolve_item(cursor, item_selector):
    item_selector = str(item_selector).strip()
    if not item_selector:
        raise InventoryInputError("Item id or name is required")

    item_id = None
    try:
        item_id = _uuid_text(item_selector, "item_id")
    except InventoryInputError:
        pass

    if item_id:
        row = cursor.execute(
            f"""
SELECT TOP (1) item_id, name, src, base_value
FROM {_table('items')}
WHERE item_id = ?
""",
            item_id,
        ).fetchone()
    else:
        row = cursor.execute(
            f"""
SELECT TOP (1) item_id, name, src, base_value
FROM {_table('items')}
WHERE name = ?
ORDER BY name
""",
            item_selector,
        ).fetchone()

    if row is None:
        raise InventoryInputError(f"Unknown item: {item_selector}")
    return _item_from_row(row)


def _get_inventory_item(cursor, user_id, item_id):
    row = cursor.execute(
        f"""
SELECT TOP (1)
    inventory.inventory_id,
    inventory.user_id,
    inventory.item_id,
    items.name,
    items.src,
    items.base_value
FROM {_table('inventory')} AS inventory
INNER JOIN {_table('items')} AS items ON items.item_id = inventory.item_id
WHERE inventory.user_id = ? AND inventory.item_id = ?
ORDER BY inventory.inventory_id
""",
        user_id,
        item_id,
    ).fetchone()

    if row is None:
        return None

    return {
        "inventory_id": str(row.inventory_id),
        "user_id": str(row.user_id),
        "item_id": str(row.item_id),
        "name": str(row.name),
        "src": str(row.src),
        "base_value": int(row.base_value),
    }


def _delete_inventory_row(cursor, inventory_id):
    cursor.execute(
        f"""
DELETE FROM {_table('inventory')}
WHERE inventory_id = ?
""",
        _uuid_text(inventory_id, "inventory_id"),
    )


def _add_to_wallet(cursor, user_id, amount):
    cursor.execute(
        f"""
UPDATE {_table('users')}
SET wallet = wallet + ?
WHERE user_id = ?
""",
        int(amount),
        _uuid_text(user_id, "user_id"),
    )
    row = cursor.execute(
        f"""
SELECT wallet
FROM {_table('users')}
WHERE user_id = ?
""",
        _uuid_text(user_id, "user_id"),
    ).fetchone()
    if row is None:
        raise InventoryInputError(f"Unknown user_id: {user_id}")
    return int(row.wallet)


def _user_from_row(row):
    return {
        "user_id": str(row.user_id),
        "name": str(row.name),
        "wallet": int(row.wallet),
    }


def _item_from_row(row):
    return {
        "item_id": str(row.item_id),
        "name": str(row.name),
        "src": str(row.src),
        "base_value": int(row.base_value),
    }
