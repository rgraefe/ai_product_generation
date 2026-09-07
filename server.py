"""Run with Python 3.11+: python server.py. Local single-user application."""
import copy
import json
import mimetypes
import os
import shutil
import threading
import traceback
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from productgen.ai import OpenAI, empty_draft, generate
from productgen.catalog import category_paths, default_rules, description_rules, ensure_export_schema, read_csv, seed_catalog
from productgen.core import build_export, refresh_derived, require, validate_config, validate_draft, validate_product
from productgen.storage import Store

ROOT = Path(__file__).resolve().parent


def load_environment():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Application:
    def __init__(self, data_dir=None, export_dir=None, provider_factory=OpenAI):
        self.store = Store(data_dir or ROOT / "data")
        catalog = self.store.config(validate_config(seed_catalog(ROOT / "BackgroundInformation")))
        if "master_columns" not in catalog["export"]:
            self.store.save_config(ensure_export_schema(catalog))
        self.export_dir = Path(export_dir or ROOT / "exports")
        self.provider_factory = provider_factory
        self.jobs = {}
        self.lock = threading.RLock()

    def idle(self, product_id):
        require(not any(j["product_id"] == product_id and j["state"] == "running" for j in self.jobs.values()), "Generation is running for this draft. Wait for it to finish.")

    def create(self, data):
        config = self.store.config()
        product = validate_product(data["product"], config)
        if not product["sku"]:
            product["sku"] = self.store.reserve_sku()
        draft = empty_draft(product, config)
        if data.get("id"):
            self.idle(data["id"])
            old = self.store.get(data["id"])
            require(old["input"]["sku"] == product["sku"], "SKU cannot change for an existing draft")
            draft["id"] = old["id"]
            return self.store.save(draft, data.get("revision"))
        draft["id"] = str(uuid.uuid4())
        return self.store.save(draft)

    def save_review(self, product_id, data):
        self.idle(product_id)
        draft = self.store.get(product_id)
        require(data.get("revision") == draft["revision"], "Draft changed in another window. Reload before saving.")
        attributes = data.get("attributes")
        require(isinstance(attributes, dict) and set(attributes) == set(draft["attributes"]), "Invalid attribute languages")
        for lang, rows in draft["attributes"].items():
            values = attributes[lang]
            require(isinstance(values, dict) and set(values) == {r["id"] for r in rows}, "Invalid attribute IDs")
            for row in rows:
                value = values[row["id"]]
                require(isinstance(value, str), "Edited values must be text")
                if value != row["value"]:
                    row.update(value=value, source="edited", confidence=None, warnings=[])
        descriptions = data.get("descriptions")
        require(isinstance(descriptions, dict) and set(descriptions) == set(draft["descriptions"]), "Invalid description languages")
        for lang, platforms in descriptions.items():
            require(isinstance(platforms, dict) and set(platforms) == set(draft["descriptions"][lang]), "Invalid description platforms")
            for fields in platforms.values():
                require(isinstance(fields, dict) and set(fields) == {"title", "description", "meta_description", "backend_keywords", "bullets"}, "Invalid description fields")
                require(all(isinstance(fields[k], str) for k in ("title", "description", "meta_description", "backend_keywords")), "Description fields must be text")
                require(isinstance(fields["bullets"], list) and all(isinstance(x, str) for x in fields["bullets"]), "Bullets must be a text list")
        draft["descriptions"] = descriptions
        draft["status"] = "draft"
        draft.pop("approved_at", None)
        refresh_derived(draft)
        validate_draft(draft)
        return self.store.save(draft, data["revision"])

    def start_generation(self, product_id, data):
        self.idle(product_id)
        draft = self.store.get(product_id)
        require(data.get("revision") == draft["revision"], "Reload the draft before generating")
        provider = self.provider_factory()
        job_id = str(uuid.uuid4())
        job = {"id": job_id, "product_id": product_id, "state": "running", "message": "Preparing generation"}
        self.jobs[job_id] = job

        def progress(message):
            with self.lock:
                job["message"] = message

        def work():
            try:
                result = generate(draft, provider, progress)
                with self.lock:
                    self.store.save(result, draft["revision"])
                    job.update(state="complete", message="Ready for review")
            except Exception as exc:
                with self.lock:
                    job.update(state="failed", message=str(exc) if isinstance(exc, ValueError) else "Generation failed. Previous draft retained; see server log.")
                if not isinstance(exc, ValueError):
                    traceback.print_exc()
        threading.Thread(target=work, daemon=True).start()
        return copy.deepcopy(job)

    def approve(self, product_id, data):
        self.idle(product_id)
        draft = self.store.get(product_id)
        require(data.get("revision") == draft["revision"], "Reload the latest draft before approval")
        require(data.get("reviewed") is True, "Confirm that you reviewed the content and warnings")
        refresh_derived(draft)
        require(not any(i["severity"] == "error" for i in validate_draft(draft)), "Correct validation errors before approval")
        draft["status"] = "approved"
        draft["approved_at"] = draft["revision"]
        return self.store.save(draft, data["revision"])

    def export(self, product_id, data):
        self.idle(product_id)
        draft = self.store.get(product_id)
        require(data.get("revision") == draft["revision"], "Reload the latest draft before export")
        settings = self.store.config()["export"]
        files = build_export(draft, settings)
        export_id = str(uuid.uuid4())
        self.export_dir.mkdir(parents=True, exist_ok=True)
        staging = self.export_dir / (".pending-" + export_id)
        destination = self.export_dir / export_id
        staging.mkdir()
        try:
            for name, content in files.items():
                (staging / name).write_bytes(content)
            manifest = {"export_id": export_id, "product_id": product_id, "sku": draft["input"]["sku"], "revision": draft["revision"],
                        "catalog_version": draft["catalog_version"], "platforms": draft["input"]["platforms"], "settings": settings,
                        "attribute_languages": draft["input"]["attribute_languages"], "description_languages": draft["input"]["description_languages"],
                        "files": list(files), "warnings": [x for x in draft["issues"] if x["severity"] == "warning"],
                        "import_notes": ["Import master data first, then categories, descriptions and attributes.",
                                         "JTL-Wawi 2.0.3: map fixed column positions once in Ameise; reuse the template for subsequent products.",
                                         "Language is identified by the filename and manifest. Attribute CSVs have exactly five fixed columns.",
                                         "All CSV column positions are stable across categories, selected platforms and languages. New fields append to the registry.",
                                         "Price is EUR; weight/dimensions are the entered grams/cm. Configure unit conversion in your Ameise mapping if required.",
                                         "Image originals remain in the local draft database. No new images are generated or imported in phase one."]}
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            snapshot = copy.deepcopy(draft)
            snapshot["input"]["images"] = [{"name": i["name"], "mime": i["mime"]} for i in snapshot["input"]["images"]]
            (staging / "reviewed_product.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            with zipfile.ZipFile(staging / "export.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(staging.iterdir()):
                    if path.name != "export.zip":
                        bundle.write(path, path.name)
            staging.rename(destination)
        except Exception:
            # Only this newly created, known staging directory can be removed.
            shutil.rmtree(staging)
            raise
        return {"id": export_id, "directory": str(destination.resolve()), "files": list(files), "download": f"/exports/{export_id}/export.zip"}


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Avoid logging submitted product content or credentials.
            if args and "jobs/" not in str(args[0]):
                super().log_message(fmt, *args)

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def safe(self, method):
            try:
                expected_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
                require(self.headers.get("Host") in expected_hosts, "Invalid local host")
                return method()
            except (ValueError, TypeError) as exc:
                self.send(400, {"error": str(exc)})
            except KeyError:
                self.send(404, {"error": "Requested resource was not found"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                traceback.print_exc()
                self.send(500, {"error": "Unexpected server error. Your previously saved data is retained; check the server log."})

        def do_GET(self):
            self.safe(self.get)

        def get(self):
            path = urlparse(self.path).path
            if path == "/api/instruction-presets":
                self.send(200, app.store.instruction_presets())
            elif path == "/api/bootstrap":
                self.send(200, {"config": app.store.config(), "products": app.store.list(), "ai": {"provider": "OpenAI", "model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"), "configured": bool(os.environ.get("OPENAI_API_KEY"))}})
            elif path.startswith("/api/products/"):
                self.send(200, app.store.get(path.split("/")[-1]))
            elif path.startswith("/api/jobs/"):
                with app.lock:
                    self.send(200, copy.deepcopy(app.jobs[path.split("/")[-1]]))
            elif path.startswith("/exports/"):
                parts = path.strip("/").split("/")
                require(len(parts) == 3 and parts[2] == "export.zip" and str(uuid.UUID(parts[1])) == parts[1], "Invalid export path")
                file = app.export_dir / parts[1] / "export.zip"
                if not file.exists():
                    raise KeyError()
                self.send(200, file.read_bytes(), "application/zip")
            elif path in ("/", "/app.js", "/style.css"):
                file = ROOT / "web" / ("index.html" if path == "/" else path[1:])
                self.send(200, file.read_bytes(), (mimetypes.guess_type(file)[0] or "application/octet-stream") + "; charset=utf-8")
            else:
                raise KeyError()

        def do_POST(self):
            self.safe(self.post)

        def post(self):
            host = self.headers.get("Host")
            require(self.headers.get("Origin", f"http://{host}") == f"http://{host}", "Cross-origin requests are not allowed")
            require(self.headers.get("X-Local-Client") == "product-studio", "Missing local client header")
            require(self.headers.get("Content-Type", "").startswith("application/json"), "JSON is required")
            length = int(self.headers.get("Content-Length", 0))
            require(0 < length <= 20 * 1024 * 1024, "Request must be between 1 byte and 20 MB")
            data = json.loads(self.rfile.read(length))
            require(isinstance(data, dict), "JSON body must be an object")
            path = urlparse(self.path).path
            with app.lock:
                if path == "/api/products":
                    result = app.create(data)
                elif path == "/api/instruction-presets":
                    result = app.store.save_instruction_preset(data)
                elif path == "/api/config":
                    result = app.store.save_config(validate_config(data))
                elif path == "/api/import-table":
                    rows = read_csv(data["csv"])
                    kind = data["kind"]
                    require(kind in ("defaults", "descriptions", "categories"), "Unknown table type")
                    result = {"rows": category_paths(rows) if kind == "categories" else description_rules(rows) if kind == "descriptions" else default_rules(rows)}
                else:
                    parts = path.strip("/").split("/")
                    require(len(parts) == 4 and parts[:2] == ["api", "products"], "Unknown endpoint")
                    product_id, action = parts[2:]
                    actions = {"review": app.save_review, "generate": app.start_generation, "approve": app.approve, "export": app.export}
                    require(action in actions, "Unknown action")
                    result = actions[action](product_id, data)
            self.send(200, result)
    return Handler


if __name__ == "__main__":
    load_environment()
    app = Application()
    port = int(os.environ.get("PRODUCT_GENERATOR_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(app))
    print(f"Product Studio: http://127.0.0.1:{port} — press Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
