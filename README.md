# Product Studio

A local, single-user product-content application for JTL-Wawi CSV workflows. It imports the supplied n8n tables, uses **OpenAI** to analyze 1–3 images and write platform-specific content, and requires human review before export.

## Run

Requires Python 3.11 or newer. There are **no third-party runtime dependencies**, frontend build steps, or database services.

1. Copy `.env.example` to `.env` and set `OPENAI_API_KEY` locally.
2. Run `python server.py`, or `./start.ps1` in PowerShell. The launcher also recognizes the Python runtime bundled with Codex on this machine.
3. Open **http://127.0.0.1:8765**.

The application also works without an API key for manual drafts, configuration, review and export. AI generation is explicitly disabled until a key is configured. `.env` changes take effect after restarting the server. `OPENAI_MODEL` defaults to `gpt-4.1-mini`; `OPENAI_VISION_MODEL` can select a separate image-analysis model. Models must support the Responses API, image input where used, and Structured Outputs. Model access depends on your API account.

The server binds to loopback only. Keep it running while using the application. This is a local tool, not an authenticated multi-user web deployment.

## Product workflow

1. Choose a category; enter title, price in EUR, size in cm, weight in grams, material and any special instructions.
2. Upload 1–3 JPEG, PNG or WebP images, at most 4 MB each.
3. Select sales channels and independent description/attribute languages. German is selected by default; English, French, Italian, Spanish, Dutch and Polish are supported.
4. Optionally enter an existing SKU, assigned EAN/GTIN, brand, dimensions or category-table override. Empty SKUs use a transaction-safe local `YYMMDD-NNN-AN` counter. The original external EAN pool is not connected; supplied GTINs are checksum-validated and never invented.
5. Choose **Generate content**, or **Save manual draft** to fill the output yourself.
6. Review all channel/language tabs and attributes. AI uncertainty, value origin, confidence and validation messages remain visible. Edit content, then **Save changes** to revalidate. Derived attributes follow their source when saved.
7. Confirm review and choose **Approve review**, then **Export CSV files**. Any later content edit revokes approval.

**Regenerate all** replaces AI-generated values and descriptions. **Edit product details** rebuilds the draft using the current configuration and clears generated content when saved. Previous revisions are retained in the local history table. Configuration changes otherwise do not alter saved drafts.

Images are analyzed together. Generation then runs once for each distinct requested language, combining attribute generation, a factual master description and selected platform descriptions. Structured response schemas reject missing/extra attributes and platforms; independent business validation enforces allowed values, required fields, content types and length limits. Optional unknown AI values remain empty and are omitted from CSV after the reviewer acknowledges warnings. No synthetic example text is presented as real AI output.

## Configuration

**Reusable special instructions:** under the product's Special instructions field, enter a preset label and choose **Save as new preset**. On any later product, choose that label and use **Use preset · replace text** or **Append to instructions**. Presets are shared across all categories and persist in the local database. Applied text remains editable for that product. **Update selected preset** saves the current instructions and label back to the preset without changing existing product drafts.

- **Attribute rules:** edit group/name, platform, default/AI/derived source, default value, instructions, type, required status, maximum length, allowed values and translation behavior. Import default or description CSVs into a reusable set. Imports merge by trimmed group + name. New sets can be assigned to categories.
- **Categories:** combine reusable sets and edit ordered routing rules using material and dimensions. Add category-path tables by importing CSV or editing paths. Unmatched sizes require an explicit selection; no silent fallback assigns a 53 cm category.
- **Platforms & settings:** globally enable/disable channels for newly created products, adjust content presets, default languages, delimiter, encoding and column-name mappings. Full catalog JSON import/export supports adding new platforms, naming sets, changing category IDs, platform ownership of paths and reordering set precedence.

The initial platforms are Shopify, Amazon, OTTO, TikTok Shop, ABOUT YOU and Kaufland. Initial length limits are project presets, not a claim of current marketplace compliance. Adapt these to your category and connector requirements.

Category sets combine in their stored order; later rules win for the same group/name. AI rules carry category filters. A generated null can retain an existing German default with an explicit warning. Derived mappings include color, pattern, dimensions, SKU, barcode and platform activation flags. A needed source attribute is generated even when its own platform is disabled, but remains internal and is excluded from export.

Human-readable defaults such as material and care can be translated; machine codes and exact allowed-value lists are preserved. A translated attribute's **name** remains its configured JTL identifier, even if it contains `de_`; map language-specific connector fields deliberately in Ameise. Explicit material/brand inputs override corresponding descriptive defaults. Review connector codes such as `SILK`, country of origin, customs codes and care defaults for each product.

### Supported CSV headers

Defaults:

```text
attribute_group,attribute_name,attribute_value
```

Dynamic descriptions:

```text
Attribute_name,Attribute_type,Attribute_format,allowed_values,JTL_group,description,Beschreibung_deutsch,Kategorie
```

Use comma-separated category IDs or `all` in `Kategorie`. Category-path tables use `Kategorie_Ebene_1` through `Kategorie_Ebene_6`. The importer accepts comma, semicolon or tab delimiters, UTF-8 BOM, quoted separators and multiline quoted values. Existing n8n ID/timestamp columns are ignored. Standard CSV imports are UTF-8; save legacy spreadsheets as UTF-8 CSV before importing.

## Export contract and JTL Ameise

Each approved export creates a new `exports/<export-id>/` directory and a downloadable ZIP. Re-exporting does not overwrite earlier bundles.

| File | Purpose |
| --- | --- |
| `Jtl_stammdaten_import.csv` | One master-data row: identifiers, input fields, JTL defaults and selected channel flags |
| `jtl_kategorien.csv` | Selected category paths, one row per path, six stable level columns |
| `Jtl_attribut_import_<language>.csv` | Exactly Artikelnummer, Artikelname, Attributgruppe, Attributname, Attributwert; also contains Amazon/OTTO bullet mappings |
| `Jtl_beschreibungen_import_<language>.csv` | One row of selected platform titles, descriptions and metadata |
| `manifest.json` | Export settings, revision, catalog version, file list and review warnings |
| `reviewed_product.json` | The reviewed content/rule snapshot, excluding image payloads |

Default format: **semicolon, UTF-8 with BOM, quoted fields and CRLF**. Decimal product inputs are emitted with a decimal comma. Identifiers remain strings. Polish and other characters that cannot be encoded in Windows-1252 cause a clear error instead of lossy export. CSV values are preserved for Ameise, including HTML source, quotes and line breaks; open/import CSVs as text to preserve SKUs and EANs.

**Target: JTL-Wawi 2.0.3, with the position-based Ameise mapping specified by the user.** Attribute files always contain exactly these five columns, in this order: `Artikelnummer, Artikelname, Attributgruppe, Attributname, Attributwert`. Every other CSV starts with `Artikelnummer, Artikelname`. Master-data and description columns use a persisted, append-only registry. Selecting different categories, platforms or languages never changes existing column positions: unused fields stay blank. New configured fields append at the end; deleting or reordering rules does not remove or move registered columns. Full catalog imports also preserve the locally registered positions.

Map these stable positions once in Ameise and reuse the template. New appended fields can be mapped when needed, without moving existing mappings. Languages are indicated by filenames and the manifest, **not extra attribute columns**. Configure language/platform selection in the corresponding import template. Functional attributes may need different treatment from ordinary multilingual attributes. Price gross/net semantics and gram/kg conversion must match your template. Optional header renaming changes only names, never order; the five fixed attribute headers cannot be renamed. Actual import execution has not been tested against the user's Wawi installation.

Master data is exported once to avoid recreating products or repeatedly updating prices for every language. Descriptions and attributes are separated by language so language-specific import templates can be used. JTL-group defaults go into master data. General text fields without an attribute group (such as `Kurzbeschreibung_Kaufland`) go into the language-specific content CSV. Source images remain attached to the draft; phase one does not generate or import additional images. For updates to existing products, configure how Ameise handles blank unused columns so unrelated content is not cleared.

No JTL process is started, no SQL connection is made to Wawi, and no external messages are sent.

## Storage and backups

- `BackgroundInformation/` remains unchanged and is read only to seed a new database.
- `data/products.sqlite3` stores the catalog, drafts, images, SKU counters and previous draft revisions. Configuration is not automatically reimported on startup.
- `exports/` contains immutable export bundles.
- `.env` contains the local API configuration and is ignored by Git. Keys are not returned to the browser or included in exports. API requests use the fixed OpenAI endpoint and `store: false`.
- Data is stored inside this project. This checkout is under OneDrive, so the operating system's synchronization settings also apply to these local files.

Stop the server before backing up the database file. Export catalog JSON from settings for a portable configuration backup. Background jobs run in the server process: closing the browser is fine, but stopping the server cancels unfinished generation and keeps the previous saved draft. A failed generation never partially replaces reviewed content.

## Verification

```powershell
python -m unittest discover -s tests -v
node --check web/app.js
```

Tests cover table imports and routing, multilingual separation, derived dependencies, activation flags, validation and approval gates, revision conflicts, transactional SKU allocation, disk export, encoding, local HTTP boundaries, and the OpenAI request/response adapter with mocked network responses. `tests/ui_server.py` starts an isolated browser-test workspace on port 8766; test fixtures and exports stay under ignored `test-results/`.

Live AI quality and actual Ameise import compatibility require an API key, representative product images and the real JTL templates. They are not established by mocked tests.

## Phase two

1. Add a Wawi/Ameise adapter for the confirmed 2.0.3 installation after obtaining the executable path, saved template IDs, instance and database. Run import commands with argument arrays, capture exit status/logs, track each bundle's import state and prevent duplicate imports. Keep credentials separate from export artifacts.
2. Decide image requirements (e.g. clean-background variants, detail crops, lifestyle compositions), model/provider, fidelity rules and review criteria. Store generated image variants with provenance and require image approval before attaching/importing them.

See [the workflow analysis](docs/workflow-analysis.md) for source findings and migration decisions.

## API references

- [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [GPT-4.1 mini capabilities](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
- [JTL Ameise import structure and mappings](https://guide.jtl-software.com/jtl-wawi/jtl-ameise/aufbau-der-import-funktion/)
- [JTL article-data import](https://guide.jtl-software.com/jtl-wawi/jtl-ameise/artikeldaten-importieren/)
