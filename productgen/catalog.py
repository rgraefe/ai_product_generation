"""Import the original n8n data without modifying the source CSVs."""
import csv
import io
import re
from pathlib import Path

LANGUAGES = {"de": "Deutsch", "en": "English", "fr": "Français", "it": "Italiano", "es": "Español", "nl": "Nederlands", "pl": "Polski"}

MASTER_COLUMNS = ["Artikelnummer", "Artikelname", "ean", "brand", "product_price", "product_weight", "product_size", "Material", "product_length", "product_width", "product_height"]
ATTRIBUTE_COLUMNS = ["Artikelnummer", "Artikelname", "Attributgruppe", "Attributname", "Attributwert"]
CATEGORY_COLUMNS = ["Artikelnummer", "Artikelname"] + [f"Kategorie_Ebene_{i}" for i in range(1, 7)]
DESCRIPTION_FIELDS = ["title", "description", "meta_description", "backend_keywords"]


def ensure_export_schema(config, previous=None):
    """Persist an append-only column registry; product selections never define positions."""
    export = config["export"]
    old = previous["export"] if previous else export
    master = list(old.get("master_columns", MASTER_COLUMNS))
    descriptions = list(old.get("description_columns", ["Artikelnummer", "Artikelname"]))
    for field in MASTER_COLUMNS:
        if field not in master:
            master.append(field)
    for s in config["sets"]:
        for r in s["rules"]:
            columns = master if r["group"].strip() == "JTL" else descriptions if not r["group"].strip() else None
            if columns is not None and r["name"].strip() not in columns:
                columns.append(r["name"].strip())
    for platform in config["platforms"]:
        for field in DESCRIPTION_FIELDS:
            name = platform["id"] + "_" + field
            if name not in descriptions:
                descriptions.append(name)
    export.update(master_columns=master, description_columns=descriptions, jtl_version="2.0.3")
    return config


def read_csv(text):
    text = text.lstrip("\ufeff")
    try:
        delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    rows = []
    for row in reader:
        if None in row:
            raise ValueError("CSV row has more values than its header")
        rows.append({k.strip(): (v or "").strip() for k, v in row.items()})
    return rows


def platform_for(group, name=""):
    text = (group + " " + name).lower()
    for needle, platform in [("amazon", "amazon"), ("shopify", "shopify"), ("maytree]:", "shopify"), ("otto", "otto"), ("intergate consulting", "otto"), ("tiktok", "tiktok"), ("kaufland", "kaufland"), ("aboutyou", "aboutyou")]:
        if needle in text:
            return platform
    return "shared"


def rule_id(group, name):
    return group.strip() + "::" + name.strip()


def default_rules(rows):
    result = {}
    human_values = {"Hauptmaterial", "Materialzusammensetzung", "Material", "Anlässe", "Zielgruppe", "Zielgruppe1", "Materialeigenschaften", "Geschlecht", "Materialart", "Pflegehinweise", "department_name", "seasons", "Art Strickwaren", "Verschlussart", "webart", "Set-Typ"}
    for row in rows:
        if not {"attribute_group", "attribute_name", "attribute_value"} <= row.keys():
            raise ValueError("Defaults CSV requires attribute_group, attribute_name, attribute_value")
        group, name = row["attribute_group"], row["attribute_name"]
        if not name:
            raise ValueError("Attribute name cannot be empty")
        result[rule_id(group, name)] = {
            "id": rule_id(group, name), "group": group, "name": name,
            "platform": platform_for(group, name), "mode": "default", "value": row["attribute_value"],
            "format": "single_line_text", "description": "", "allowed_values": [],
            "max_length": None, "required": False, "localize": name in human_values,
        }
    return list(result.values())


def description_rules(rows):
    result = {}
    for row in rows:
        if not {"Attribute_name", "Attribute_format", "Kategorie"} <= row.keys():
            raise ValueError("Description CSV requires Attribute_name, Attribute_format, Kategorie")
        group, name = row.get("JTL_group", ""), row["Attribute_name"]
        key = rule_id(group, name)
        rule = {"id": key, "group": group, "name": name, "platform": platform_for(group, name),
                "mode": "ai", "value": "", "format": row["Attribute_format"] or "single_line_text",
                "description": row.get("Beschreibung_deutsch") or row.get("description", ""),
                "allowed_values": [x.strip() for x in row.get("allowed_values", "").split(",") if x.strip()],
                "max_length": None, "required": False, "localize": True,
                "categories": [x.strip().lower() for x in row["Kategorie"].split(",") if x.strip()]}
        if key in result:
            previous = result[key]
            previous["categories"] = sorted(set(previous["categories"] + rule["categories"]))
            if rule["description"] not in previous["description"]:
                previous["description"] += "\n\n" + rule["description"]
        else:
            result[key] = rule
    return list(result.values())


def category_paths(rows):
    if rows and "Kategorie_Ebene_1" not in rows[0]:
        raise ValueError("Category CSV requires Kategorie_Ebene_1 ... Kategorie_Ebene_6")
    result = []
    for row in rows:
        path = [row.get(f"Kategorie_Ebene_{i}", "") for i in range(1, 7)]
        while path and not path[-1]:
            path.pop()
        if not path or any(not x for x in path):
            raise ValueError("Category paths must be non-empty and have no gaps")
        result.append({"platform": "shopify" if path[0].lower() == "shopify" else "shared", "levels": path})
    return result


def seed_catalog(background: Path):
    tables = {}
    for path in sorted(background.glob("*.csv")):
        tables[path.stem] = read_csv(path.read_text(encoding="utf-8-sig"))
    defaults = {}
    for name, rows in tables.items():
        if name.startswith("Standard_"):
            set_id = re.sub(r" \(\d+\)$", "", name)
            defaults[set_id] = {"id": set_id, "name": set_id.replace("Standard_Attributes_", ""), "rules": default_rules(rows), "source": name + ".csv"}
    dynamic = description_rules(next(rows for name, rows in tables.items() if name.startswith("AttributBeschreibung")))
    defaults["ai_attributes"] = {"id": "ai_attributes", "name": "AI attribute descriptions", "rules": dynamic, "source": "AttributBeschreibung (3).csv"}
    # Preserve the workflow's derived attributes as explicit, editable rules.
    derived = []
    bindings = [
        ("Artikel", "Breite", "input.width"), ("Artikel", "Länge", "input.length"),
        ("Shopify-Attribute", "sku", "input.sku"), ("Shopify-Attribute", "barcode", "input.ean"),
        ("unicorn2-OttoMarket", "Gewicht", "input.weight"), ("AmazonLister", "shipping_weight", "input.weight"),
        ("AmazonLister", "size", "input.size"), ("SXC-OttoMarket", "Reference", "input.sku"),
        ("AmazonLister", "color_name", "attribute.Artikel::Farbe"), ("Shopify-Attribute", "Color", "attribute.Artikel::Farbe"),
        ("Shopify-Metafields", "meta_color:mm-google-shopping", "attribute.Artikel::Farbe"),
        ("Shopify-Metafields", "meta_color:custom", "attribute.Artikel::Farbe"),
        ("TikTok", "DesignSchal", "attribute.unicorn2-OttoMarket::Optik_ai"),
        ("unicorn2-OttoMarket", "Optik", "attribute.unicorn2-OttoMarket::Optik_ai"),
        ("unicorn2-OttoMarket", "Stil", "attribute.unicorn2-OttoMarket::Optik_ai"),
        ("AmazonLister", "Muster", "attribute.unicorn2-OttoMarket::Optik_ai"),
        ("AmazonLister", "model", "attribute.unicorn2-OttoMarket::Optik_ai"),
        ("Shopify-Metafields", "meta_pattern:custom", "attribute.unicorn2-OttoMarket::Optik_ai"),
    ]
    for group, name, binding in bindings:
        r = default_rules([{"attribute_group": group, "attribute_name": name, "attribute_value": ""}])[0]
        r.update(mode="derived", binding=binding, localize=binding.startswith("attribute."))
        derived.append(r)
    defaults["derived"] = {"id": "derived", "name": "Product fields & marketplace mappings", "rules": derived, "source": "ArtikelAttribute workflow node"}
    categories = []
    for cat_id, label, default in [("seidentuch-damen", "Seidentuch · Damen", "Seide_weiblich"), ("seidentuch-herren", "Seidentuch · Herren", "Seide_männlich"), ("armband", "Armband", "Armband"), ("brosche", "Brosche", "Brosche"), ("kette", "Kette", "Kette"), ("ohrring", "Ohrring", "Ohrring")]:
        routes = []
        if cat_id.startswith("seidentuch"):
            routes.append({"material_contains": "baumwolle", "table": "KategorienSeideBaumwolle"})
            if cat_id.endswith("herren"):
                routes.append({"sizes": [53], "table": "KategorienSeide53Herren"})
            else:
                for sizes, table in [([5], "Bandeau"), ([86], "86"), ([70], "70"), ([65, 64], "65"), ([55], "55"), ([16], "16"), ([53], "53")]:
                    routes.append({"sizes": sizes, "table": "KategorienSeide" + table})
        else:
            routes.append({"table": "Kategorien" + label})
        categories.append({"id": cat_id, "name": label, "sets": ["Standard_Attributes_" + default, "ai_attributes", "derived"], "routes": routes})
    platforms = []
    for pid, name, title, description, bullets, instruction in [
        ("shopify", "Shopify", 255, 5000, 0, "Elegant, approachable product copy. Simple HTML only: p, ul, li, strong. Include meta_description."),
        ("amazon", "Amazon", 200, 2000, 5, "Factual, searchable title. Plain text. Five distinct factual bullet points. Include space-separated backend_keywords without repetitions."),
        ("otto", "OTTO", 200, 1000, 5, "Neutral catalog style, plain text. Five factual bullets. Care instructions only when supplied."),
        ("tiktok", "TikTok Shop", 80, 3000, 0, "Concise, friendly factual copy. Include confirmed size and color. Plain text."),
        ("aboutyou", "ABOUT YOU", 200, 3000, 0, "Neutral fashion catalog description, plain text."),
        ("kaufland", "Kaufland", 200, 5000, 0, "Factual product description and relevant search terms, plain text."),
    ]:
        platforms.append({"id": pid, "name": name, "enabled": True, "title_max": title, "description_max": description,
                          "bullet_count": bullets, "bullet_max": 120 if pid == "otto" else 255,
                          "meta_max": 160, "keywords_max": 1000, "instructions": instruction})
    return ensure_export_schema({"version": 1, "languages": LANGUAGES, "attribute_languages": ["de"], "description_languages": ["de"],
            "platforms": platforms, "categories": categories, "sets": list(defaults.values()),
            "category_tables": {name: category_paths(rows) for name, rows in tables.items() if name.startswith("Kategorien")},
            "export": {"delimiter": ";", "encoding": "utf-8-sig", "column_mapping": {}},
            "import_notes": ["Trimmed whitespace in group and attribute names.", "Merged duplicate Besondere Merkmale descriptions.",
                             "Platform limits are editable project presets, not verified marketplace policy.",
                             "Machine codes and default values are not translated unless Localize is enabled.",
                             "Unmatched sizes require a category-table selection; no silent 53 cm fallback."]})
