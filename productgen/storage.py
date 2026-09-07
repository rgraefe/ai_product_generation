import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .core import require
from .catalog import ensure_export_schema


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "products.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, sku TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS counters (day TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, product_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS instruction_presets (id TEXT PRIMARY KEY, label_key TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def config(self, initial=None):
        with self.connection() as db:
            row = db.execute("SELECT body FROM config WHERE id=1").fetchone()
            if row:
                return json.loads(row[0])
            require(initial is not None, "Missing catalog")
            db.execute("INSERT INTO config VALUES (1, ?)", (json.dumps(initial, ensure_ascii=False),))
            return initial

    def save_config(self, config):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = json.loads(db.execute("SELECT body FROM config WHERE id=1").fetchone()[0])
            require(config["version"] == old["version"], "Configuration changed in another window. Reload it first.")
            ensure_export_schema(config, old)
            config["version"] += 1
            db.execute("UPDATE config SET body=? WHERE id=1", (json.dumps(config, ensure_ascii=False),))
        return config

    def reserve_sku(self):
        day = datetime.now().strftime("%y%m%d")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            while True:
                db.execute("INSERT INTO counters VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET value=value+1", (day,))
                n = db.execute("SELECT value FROM counters WHERE day=?", (day,)).fetchone()[0]
                sku = f"{day}-{n:03d}-AN"
                if not db.execute("SELECT 1 FROM products WHERE sku=?", (sku,)).fetchone():
                    return sku

    def instruction_presets(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM instruction_presets ORDER BY label_key")]

    def save_instruction_preset(self, data):
        label, instructions = data.get("label"), data.get("instructions")
        require(isinstance(label, str) and 0 < len(label.strip()) <= 120, "Enter a preset label (1–120 characters)")
        require(isinstance(instructions, str) and 0 < len(instructions.strip()) <= 20000, "Enter instructions (1–20,000 characters)")
        preset_id = data.get("id")
        require(preset_id is None or isinstance(preset_id, str), "Invalid preset ID")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = 1
            if preset_id:
                row = db.execute("SELECT body FROM instruction_presets WHERE id=?", (preset_id,)).fetchone()
                require(row is not None, "Preset no longer exists. Reload the presets.")
                old = json.loads(row[0])
                require(data.get("revision") == old["revision"], "Preset changed in another window. Reload the presets before updating.")
                revision = old["revision"] + 1
            else:
                preset_id = str(uuid.uuid4())
            preset = {"id": preset_id, "label": label.strip(), "instructions": instructions.strip(), "revision": revision}
            try:
                db.execute("INSERT INTO instruction_presets VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET label_key=excluded.label_key, body=excluded.body",
                           (preset_id, preset["label"].casefold(), json.dumps(preset, ensure_ascii=False)))
            except sqlite3.IntegrityError:
                raise ValueError("That preset label already exists. Choose another label or update the selected preset.") from None
        return preset

    def get(self, product_id):
        with self.connection() as db:
            row = db.execute("SELECT body FROM products WHERE id=?", (product_id,)).fetchone()
            if not row:
                raise KeyError("Product not found")
            return json.loads(row[0])

    def list(self):
        with self.connection() as db:
            drafts = [json.loads(r[0]) for r in db.execute("SELECT body FROM products ORDER BY rowid DESC")]
        return [{k: d[k] for k in ("id", "status", "revision", "updated_at")} | {"sku": d["input"]["sku"], "title": d["input"]["title"], "platforms": d["input"]["platforms"], "generation": d["generation"]} for d in drafts]

    def save(self, draft, expected_revision=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old_row = db.execute("SELECT body FROM products WHERE id=?", (draft["id"],)).fetchone()
            if old_row:
                old = json.loads(old_row[0])
                require(expected_revision == old["revision"], "Draft changed in another window. Reload before saving.")
                db.execute("INSERT INTO history(product_id, revision, body, created_at) VALUES (?,?,?,?)", (draft["id"], old["revision"], old_row[0], old["updated_at"]))
                draft["revision"] = old["revision"] + 1
            else:
                require(expected_revision is None, "Draft no longer exists")
                draft["revision"] = 1
            draft["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            try:
                db.execute("INSERT INTO products VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (draft["id"], draft["input"]["sku"], json.dumps(draft, ensure_ascii=False, allow_nan=False)))
            except sqlite3.IntegrityError:
                raise ValueError("This SKU already exists locally. Open its draft or choose a different SKU.") from None
        return draft
