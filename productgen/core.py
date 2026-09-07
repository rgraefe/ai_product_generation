import base64
import copy
import csv
import io
import json
import math
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from .catalog import ATTRIBUTE_COLUMNS, CATEGORY_COLUMNS, LANGUAGES, rule_id


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_config(config):
    require(isinstance(config, dict), "Configuration must be an object")
    for key in ("platforms", "categories", "sets"):
        require(isinstance(config.get(key), list) and config[key], f"{key} must be a non-empty list")
        ids = [r.get("id") for r in config[key]]
        require(all(isinstance(x, str) and x.strip() for x in ids) and len(ids) == len(set(ids)), f"{key}: IDs must be unique")
    platforms = {p["id"] for p in config["platforms"]}
    require(all(re.fullmatch(r"[a-z0-9_-]+", p) for p in platforms) and "shared" not in platforms, "Platform IDs must be lowercase slugs (shared is reserved)")
    for p in config["platforms"]:
        for field in ("title_max", "description_max", "bullet_max", "meta_max", "keywords_max"):
            require(type(p.get(field)) is int and p[field] > 0, f"{p['id']}: {field} must be positive")
        require(type(p.get("bullet_count")) is int and 0 <= p["bullet_count"] <= 10, "Bullet count must be 0–10")
        require(isinstance(p.get("instructions"), str) and isinstance(p.get("name"), str) and type(p.get("enabled")) is bool, "Invalid platform settings")
    for key in ("attribute_languages", "description_languages"):
        validate_languages(config.get(key), key)
    category_ids = {c["id"] for c in config["categories"]}
    for s in config["sets"]:
        require(isinstance(s.get("rules"), list), "Attribute set requires rules")
        seen = set()
        for r in s["rules"]:
            require(isinstance(r.get("group"), str) and isinstance(r.get("name"), str) and r["name"].strip(), "Invalid attribute identity")
            r["group"], r["name"] = r["group"].strip(), r["name"].strip()
            r["id"] = rule_id(r["group"], r["name"])
            require(r["id"] not in seen, f"Duplicate attribute in {s['id']}: {r['id']}")
            seen.add(r["id"])
            require(r.get("platform") in platforms | {"shared"}, "Unknown attribute platform")
            require(r.get("mode") in ("default", "ai", "derived"), "Unknown attribute mode")
            require(r.get("format") in ("single_line_text", "multi_line_text", "number", "boolean"), "Unknown attribute format")
            require(r.get("max_length") is None or type(r["max_length"]) is int and r["max_length"] > 0, "Maximum length must be positive or empty")
            require(isinstance(r.get("allowed_values"), list) and all(isinstance(x, str) for x in r["allowed_values"]), "Allowed values must be a text list")
            require(isinstance(r.get("description"), str) and isinstance(r.get("value"), (str, int, float, bool)), "Invalid rule description or default")
            require(type(r.get("required")) is bool and type(r.get("localize")) is bool, "Rule flags must be true/false")
            require(isinstance(r.get("categories", []), list) and set(r.get("categories", [])) <= category_ids | {"all"}, "Unknown rule category")
            if r["mode"] == "derived":
                require(isinstance(r.get("binding"), str) and r["binding"].startswith(("input.", "attribute.")), "Derived rule needs an input. or attribute. binding")
    set_ids = {s["id"] for s in config["sets"]}
    require(isinstance(config.get("category_tables"), dict), "Category tables must be an object")
    for name, paths in config["category_tables"].items():
        require(isinstance(paths, list) and paths, f"{name}: category table is empty")
        for path in paths:
            require(path.get("platform") in platforms | {"shared"}, "Unknown category platform")
            require(isinstance(path.get("levels"), list) and 1 <= len(path["levels"]) <= 6 and all(isinstance(x, str) and x.strip() for x in path["levels"]), "Category path requires 1–6 non-empty levels")
    for c in config["categories"]:
        require(isinstance(c.get("sets"), list) and c["sets"] and set(c["sets"]) <= set_ids, "Category references unknown/empty sets")
        require(isinstance(c.get("routes"), list), "Category requires routing rules")
        for route in c["routes"]:
            require(route.get("table") in config["category_tables"], "Route references unknown category table")
            require(isinstance(route.get("material_contains", ""), str), "Material condition must be text")
            require(isinstance(route.get("sizes", []), list) and all(type(x) in (int, float) and math.isfinite(x) and x > 0 for x in route.get("sizes", [])), "Sizes must be positive numbers")
    export = config.get("export", {})
    require(export.get("delimiter") in (";", ",", "\t"), "Unsupported CSV delimiter")
    require(export.get("encoding") in ("utf-8-sig", "utf-8", "cp1252"), "Unsupported CSV encoding")
    require(isinstance(export.get("column_mapping"), dict) and all(isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in export["column_mapping"].items()), "Column mapping must map names to non-empty names")
    for key in ("master_columns", "description_columns"):
        columns = export.get(key, [])
        require(isinstance(columns, list) and columns[:2] == ["Artikelnummer", "Artikelname"] and all(isinstance(x, str) and x.strip() for x in columns) and len(columns) == len(set(columns)), "Export column registry must start with Artikelnummer, Artikelname and have unique names")
    require(all(export["column_mapping"].get(key, key) == key for key in ATTRIBUTE_COLUMNS), "The five fixed JTL attribute headers cannot be renamed")
    # Verify composition and cycles before any generation request can use the catalog.
    for c in config["categories"]:
        compose(config, {"category": c["id"], "platforms": list(platforms)})
    return config


def validate_languages(languages, label):
    require(isinstance(languages, list) and languages and all(isinstance(x, str) and x in LANGUAGES for x in languages), f"Choose supported {label}")
    require(len(languages) == len(set(languages)), f"Duplicate {label}")


def numeric(value, name, optional=False):
    if optional and (value is None or value == ""):
        return ""
    try:
        n = Decimal(str(value).replace(",", "."))
        require(n.is_finite() and n >= 0, f"{name} must be a non-negative finite number")
        return format(n, "f")
    except InvalidOperation:
        raise ValueError(f"{name} must be a number") from None


def validate_product(data, config):
    p = copy.deepcopy(data)
    require(p.get("category") in {c["id"] for c in config["categories"]}, "Choose a category")
    for key in ("title", "material", "size"):
        require(isinstance(p.get(key), str) and p[key].strip(), f"{key} is required")
        p[key] = p[key].strip()
    for key in ("sku", "ean", "brand", "instructions", "category_table"):
        require(isinstance(p.get(key, ""), str), f"{key} must be text")
        p[key] = p.get(key, "").strip()
    for key in ("price", "weight"):
        p[key] = numeric(p.get(key), key)
    for key in ("width", "length", "height"):
        p[key] = numeric(p.get(key), key, optional=True)
    if p["ean"]:
        require(p["ean"].isdigit() and len(p["ean"]) in (8, 12, 13, 14), "EAN/GTIN must have 8, 12, 13 or 14 digits")
        digits = list(map(int, p["ean"]))
        checksum = sum(n * (3 if i % 2 == 0 else 1) for i, n in enumerate(reversed(digits[:-1])))
        require((10 - checksum % 10) % 10 == digits[-1], "EAN/GTIN checksum is invalid")
    require(isinstance(p.get("platforms"), list) and p["platforms"] and len(p["platforms"]) == len(set(p["platforms"])), "Choose at least one platform, without duplicates")
    require(set(p["platforms"]) <= {r["id"] for r in config["platforms"] if r["enabled"]}, "Unknown or disabled platform")
    for key in ("attribute_languages", "description_languages"):
        validate_languages(p.get(key), key)
    require(isinstance(p.get("images"), list) and 1 <= len(p["images"]) <= 3, "Upload 1–3 product images")
    for image in p["images"]:
        require(isinstance(image, dict) and isinstance(image.get("name"), str), "Invalid image")
        require(image.get("mime") in ("image/jpeg", "image/png", "image/webp"), "Use JPEG, PNG or WebP images")
        try:
            raw = base64.b64decode(image["data"], validate=True)
        except (ValueError, KeyError, TypeError):
            raise ValueError("Invalid image data") from None
        require(0 < len(raw) <= 4 * 1024 * 1024, "Each image must be at most 4 MB")
        signatures = {"image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"), "image/jpeg": raw.startswith(b"\xff\xd8\xff"), "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"}
        require(signatures[image["mime"]], "Image content does not match its type")
    return p


def compose(config, product):
    category = next(c for c in config["categories"] if c["id"] == product["category"])
    sets = {s["id"]: s for s in config["sets"]}
    merged = {}
    for set_id in category["sets"]:
        for original in sets[set_id]["rules"]:
            if original.get("categories") and not {"all", product["category"]}.intersection(original["categories"]):
                continue
            r = copy.deepcopy(original)
            previous = merged.get(r["id"])
            if previous and previous["mode"] == "default" and r["mode"] == "ai":
                r["fallback"] = previous["value"]
            r["set"] = set_id
            merged[r["id"]] = r
    visible = {k for k, r in merged.items() if r["platform"] == "shared" or r["platform"] in product["platforms"] or r["name"].startswith("Verkaufskanal [")}
    needed = set(visible)

    def dependencies(key, chain):
        require(key not in chain, f"Cyclic derived attribute: {key}")
        r = merged[key]
        binding = r.get("binding", "")
        if r["mode"] == "derived" and binding.startswith("attribute."):
            dep = binding.removeprefix("attribute.")
            require(dep in merged, f"Missing derived source: {dep}")
            needed.add(dep)
            dependencies(dep, chain | {key})
    for key in visible:
        dependencies(key, set())
    return [dict(r, export=key in visible) for key, r in merged.items() if key in needed]


def resolve_paths(config, p):
    table = p.get("category_table", "")
    if table:
        require(table in config["category_tables"], "Unknown category table")
    else:
        c = next(c for c in config["categories"] if c["id"] == p["category"])
        dimensions = [float(p[k]) for k in ("width", "length") if p.get(k) != ""]
        if not dimensions:
            dimensions = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", p["size"])]
        for route in c["routes"]:
            if route.get("material_contains") and route["material_contains"].lower() not in p["material"].lower():
                continue
            if route.get("sizes") and not set(route["sizes"]).intersection(dimensions):
                continue
            table = route["table"]
            break
    require(bool(table), "No matching category path. Choose a category table explicitly or add a routing rule.")
    paths = [copy.deepcopy(path) for path in config["category_tables"][table] if path["platform"] == "shared" or path["platform"] in p["platforms"]]
    require(bool(paths), "No category paths for the selected platforms")
    return table, paths


def value_text(value):
    if value is None:
        return ""
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def value_errors(rule, value):
    errors = []
    text = value_text(value)
    if not text.strip():
        return ["Required value is empty"] if rule["required"] else []
    if rule["max_length"] and len(text) > rule["max_length"]:
        errors.append(f"{len(text)} characters; maximum {rule['max_length']}")
    if rule["allowed_values"] and text not in rule["allowed_values"]:
        errors.append("Value is not in the allowed list")
    if rule["format"] == "single_line_text" and ("\n" in text or "\r" in text):
        errors.append("Line breaks are not allowed")
    if rule["format"] == "number":
        try:
            if not Decimal(text.replace(",", ".")).is_finite():
                raise InvalidOperation
        except InvalidOperation:
            errors.append("A finite number without units is required")
    if rule["format"] == "boolean" and text not in ("true", "false"):
        errors.append("Use true or false")
    if text.startswith("ERROR!"):
        errors.append("Workflow error text must be corrected")
    return errors


class ProductHTML(HTMLParser):
    """Only the formatting tags allowed by our Shopify content contract."""
    def __init__(self):
        super().__init__()
        self.invalid = False

    def handle_starttag(self, tag, attrs):
        if tag not in {"p", "ul", "li", "strong"} or attrs:
            self.invalid = True

    def handle_endtag(self, tag):
        if tag not in {"p", "ul", "li", "strong"}:
            self.invalid = True

    def handle_decl(self, decl):
        self.invalid = True

    def handle_pi(self, data):
        self.invalid = True


def refresh_derived(draft):
    p = draft["input"]
    for lang, rows in draft["attributes"].items():
        by_id = {r["id"]: r for r in rows}
        visited = set()

        def resolve(row):
            if row["id"] in visited:
                return
            rule = row["rule"]
            if rule["name"].startswith("Verkaufskanal ["):
                row["value"] = "Y" if rule["platform"] in p["platforms"] else "N"
                row["source"] = "platform selection"
            elif rule["mode"] == "derived":
                binding = rule["binding"]
                if binding.startswith("input."):
                    row["value"] = value_text(p.get(binding[6:], ""))
                else:
                    source = by_id[binding[10:]]
                    resolve(source)
                    row["value"] = source["value"]
                row["source"] = "derived"
            visited.add(row["id"])
        for row in rows:
            resolve(row)


def validate_draft(draft):
    issues = []
    for lang, rows in draft["attributes"].items():
        for row in rows:
            errors = value_errors(row["rule"], row["value"])
            issues.extend({"location": f"{lang} · {row['id']}", "message": e, "severity": "error"} for e in errors)
            if not value_text(row["value"]).strip() and row["rule"]["mode"] == "ai":
                issues.append({"location": f"{lang} · {row['id']}", "message": "Uncertain / empty optional attribute; omitted from CSV", "severity": "warning"})
            for warning in row.get("warnings", []):
                issues.append({"location": f"{lang} · {row['id']}", "message": warning, "severity": "warning"})
            if row["rule"]["mode"] == "default" and row["rule"]["localize"] and lang != "de" and not value_text(row["value"]).strip():
                issues.append({"location": f"{lang} · {row['id']}", "message": "Default content needs a translation or an explicit manual value", "severity": "error"})
    for lang, platforms in draft["descriptions"].items():
        for pid, fields in platforms.items():
            rules = draft["platform_rules"][pid]
            for field, limit in [("title", "title_max"), ("description", "description_max"), ("meta_description", "meta_max"), ("backend_keywords", "keywords_max")]:
                value = fields.get(field, "")
                needed = field in ("title", "description") or (field == "meta_description" and pid == "shopify") or (field == "backend_keywords" and pid == "amazon")
                message = "Required content is empty" if needed and not value.strip() else f"{len(value)} characters; maximum {rules[limit]}" if len(value) > rules[limit] else ""
                if message:
                    issues.append({"location": f"{lang} · {pid} · {field}", "message": message, "severity": "error"})
                if pid == "shopify" and field == "description":
                    parser = ProductHTML()
                    parser.feed(value)
                    if parser.invalid:
                        issues.append({"location": f"{lang} · {pid} · {field}", "message": "Use only p, ul, li and strong HTML tags without attributes", "severity": "error"})
                elif re.search(r"<[^>]+>", value):
                    issues.append({"location": f"{lang} · {pid} · {field}", "message": "Plain text is required", "severity": "error"})
                if "ERROR!" in value:
                    issues.append({"location": f"{lang} · {pid} · {field}", "message": "Error text must be corrected", "severity": "error"})
            bullets = fields.get("bullets", [])
            if len(bullets) != rules["bullet_count"]:
                issues.append({"location": f"{lang} · {pid} · bullets", "message": f"Expected {rules['bullet_count']} bullets", "severity": "error"})
            for index, bullet in enumerate(bullets):
                if not bullet.strip() or len(bullet) > rules["bullet_max"] or re.search(r"<[^>]+>", bullet):
                    issues.append({"location": f"{lang} · {pid} · bullet {index + 1}", "message": f"Bullet must contain 1–{rules['bullet_max']} characters", "severity": "error"})
    draft["issues"] = issues
    return issues


def csv_bytes(rows, headers, settings):
    mapping = settings["column_mapping"]
    targets = [mapping.get(h, h) for h in headers]
    require(len(targets) == len(set(targets)), "Column mapping produces duplicate CSV headers")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=settings["delimiter"], lineterminator="\r\n", quoting=csv.QUOTE_ALL)
    writer.writerow(targets)
    for row in rows:
        writer.writerow([value_text(row.get(h, "")) for h in headers])
    try:
        return stream.getvalue().encode(settings["encoding"])
    except UnicodeEncodeError:
        raise ValueError("Content cannot be represented in Windows-1252. Choose UTF-8 for multilingual exports.") from None


def build_export(draft, settings):
    require(draft["status"] == "approved", "Approve the current draft before export")
    require(not any(i["severity"] == "error" for i in validate_draft(draft)), "Resolve validation errors before export")
    p = draft["input"]
    sku = p["sku"]
    files = {}
    base = {"Artikelnummer": sku, "Artikelname": p["title"], "ean": p["ean"], "brand": p["brand"],
            "product_price": p["price"].replace(".", ","), "product_weight": p["weight"].replace(".", ","), "product_size": p["size"],
            "Material": p["material"], "product_length": p["length"].replace(".", ","), "product_width": p["width"].replace(".", ","), "product_height": p["height"].replace(".", ",")}
    # Master data is written once, avoiding repeated creation/price updates across languages.
    first_language = "de" if "de" in draft["attributes"] else next(iter(draft["attributes"]))
    for row in draft["attributes"][first_language]:
        if row["rule"]["export"] and row["rule"]["group"] == "JTL" and value_text(row["value"]).strip():
            base[row["rule"]["name"]] = row["value"]
    files["Jtl_stammdaten_import.csv"] = csv_bytes([base], settings["master_columns"], settings)
    category_rows = [{"Artikelnummer": sku, "Artikelname": p["title"], **{f"Kategorie_Ebene_{i + 1}": level for i, level in enumerate(path["levels"])}} for path in draft["category_paths"]]
    files["jtl_kategorien.csv"] = csv_bytes(category_rows, CATEGORY_COLUMNS, settings)
    for lang in sorted(set(draft["attributes"]) | set(draft["descriptions"])):
        attrs = {}
        content = {"Artikelnummer": sku, "Artikelname": p["title"]}
        for row in draft["attributes"].get(lang, []):
            r, value = row["rule"], row["value"]
            if r["export"] and not r["group"] and value_text(value).strip():
                content[r["name"]] = value
            elif r["export"] and r["group"] != "JTL" and value_text(value).strip():
                attrs[r["id"]] = {"Artikelnummer": sku, "Artikelname": p["title"], "Attributgruppe": r["group"], "Attributname": r["name"], "Attributwert": value}
        for pid, fields in draft["descriptions"].get(lang, {}).items():
            for name, value in fields.items():
                if name != "bullets":
                    content[f"{pid}_{name}"] = value
            if pid in ("amazon", "otto"):
                group = "Amazon Bulletpoints" if pid == "amazon" else "SXC-OttoMarket"
                for i, bullet in enumerate(fields["bullets"], 1):
                    name = f"{lang}_bullet{i}" if pid == "amazon" else f"BulletPoint{i}"
                    attrs[rule_id(group, name)] = {"Artikelnummer": sku, "Artikelname": p["title"], "Attributgruppe": group, "Attributname": name, "Attributwert": bullet}
        if attrs:
            files[f"Jtl_attribut_import_{lang}.csv"] = csv_bytes(list(attrs.values()), ATTRIBUTE_COLUMNS, settings)
        if lang in draft["descriptions"] or len(content) > 2:
            files[f"Jtl_beschreibungen_import_{lang}.csv"] = csv_bytes([content], settings["description_columns"], settings)
    return files
