# Analysis of the supplied n8n product generator

Analyzed: the 53 nodes in `BackgroundInformation/My workflow no Base64 webhook2.json` and all 20 supplied CSV tables. Source files are retained unchanged.

## Existing data flow

1. `Webhook1` receives product fields/images. `Edit Fields1` normalizes input, assembles size from dimensions and hard-codes the MayTree brand and several product defaults.
2. `GetSKU1` allocates a daily PostgreSQL counter in `YYMMDD-NNN-AN` format. `GetEAN1` atomically marks an unused `ean_pool` record with the SKU. These databases are external dependencies, not supplied data tables.
3. `Switch` chooses one of six standard-attribute tables: female/male silk scarves, bracelets, brooches, necklaces or earrings.
4. `KategorieTableID` selects a category table by material/category/size. `Kategorien` produces category rows. The workflow writes categories to disk before the remaining content workflow completes.
5. Gemini `Analyze an image` derives visual facts and uncertainties. `Product Understanding` parses JSON and applies partial structural checks. The configured image model is `models/nano-banana-pro-preview`; later generation nodes use `models/gemini-2.5-flash`.
6. `attribute_descriptions` and `FilterRequiredAttributes` select AI attribute definitions using `Kategorie` (`all` or category IDs). The item loop calls `GetAttributeValues` separately for each dynamic attribute.
7. Defaults and generated attributes are normalized and merged. Non-null generated values override defaults by group + attribute name.
8. `MasterContent` writes a neutral German master description, validates it and writes it to PostgreSQL. Five platform branches generate Shopify, Amazon, OTTO, TikTok and ABOUT YOU copy. Kaufland appears in dynamic attributes and activation flags, but has no dedicated description-generation branch.
9. `ArtikelStammdaten`, `ArtikelAttribute` and `Kategorien` construct three semicolon-delimited CSVs. Telegram sends a completion message. Several SQL, table-ID, channel-ID and filesystem dependencies are deployment-specific.

## Supplied table inventory

| Family | Files | Rows | Role |
| --- | ---: | ---: | --- |
| Silk female defaults | 1 | 45 | JTL, Amazon, Shopify and OTTO constants |
| Silk male defaults | 1 | 48 | Male scarf constants and connector fields |
| Jewelry defaults | 4 | 28 each | Bracelet, brooch, necklace and earring constants |
| Dynamic attribute descriptions | 1 | 23 | Generation intent, type, format, allowed values and applicability |
| Category-path tables | 13 | Variable | Up to six levels per JTL category path |

Normalization produces **205 default rules**, **22 unique AI rules**, and **18 explicit derived mappings**: **245 rules across eight reusable sets**. Six initial product categories combine these sets. A rule can be shared or assigned to a platform; platform-specific fields are filtered per product, with internal dependencies retained only when needed.

## Findings that affect correctness

| Finding | Evidence / effect | Implemented treatment |
| --- | --- | --- |
| Whitespace in identities | Several attribute names/groups have trailing LF/CRLF; male Shopify group includes a trailing space | Trim identities during CSV import |
| Duplicate dynamic definition | `Besondere Merkmale` occurs twice for the same group | Merge instructions/applicability into one rule |
| Broken allowed-values validation | `ValidateAttribut` checks `obj.value`, but the output contract uses `attribute_value` | Validate the actual value against the configured enum |
| Partial schema enforcement | Many required scalar/array types are not fully checked; model confidence is not consistently range-checked | Strict OpenAI schema plus independent server validation |
| Errors inserted into product copy | Fail-soft platform validators prefix field values with `ERROR!` and the original content | Keep errors separate and block approval/export on invalid content |
| German is hard-coded | German prompts/heuristics override or contradict `language` inputs | Explicit independent language selections; no German-marker heuristic |
| Contradictory TikTok instructions | Emotional text with two paragraphs is also required to be neutral and one paragraph | One editable instruction preset per platform |
| Forced unsupported facts | OTTO prompt always requests scarf-size formatting and material-based care instructions, even for jewelry | Use only confirmed input/default facts and applicable category rules |
| Category material-field mismatch | Routing reads `product_material`, while normalized fields use `Material` | Normalize one material input and route from it |
| Silent category fallback | Unknown dimensions eventually map to `KategorienSeide53` | Require an explicit route/override on unmatched sizes |
| Cotton condition is overbroad | Material branch precedes product-category checks | Apply cotton routing only inside the scarf categories |
| All channels always active | JTL default rows unconditionally set several specific sales-channel fields to `Y` | Set known flags to Y/N from the selected platform list |
| Missing platform selection | Description branches always run and attribute groups are not filtered | Generate/export selected channels only |
| Name-only secondary lookup | `attributes_kv` discards group identity, risking collisions | Group + name remains the canonical key everywhere |
| Duplicate width/length mapping | `ArtikelAttribute` pushes `Artikel/Länge` twice | One declared mapping per identity |
| Swapped optional jewelry fields | `durchmesser_anhaenger` feeds `Perlengröße`, and `perlengroesse` feeds `Durchmesser Anhänger` | Do not carry the swapped mappings into phase one; optional category fields can be added as rules |
| Barcode spelling inconsistency | Defaults use `barcode_type`; manual mapping adds `barcode_typ` | Keep the imported `barcode_type` plus assigned barcode value |
| ABOUT YOU key mismatch | Prompt uses `aboutyou-description`; master-data flattening expects `_description` | Stable configured `aboutyou` platform key |
| Premature partial outputs | Category CSV is written upstream of AI failures and review | Export only the approved complete snapshot |
| No review persistence | Export follows generation; generated values cannot be corrected in an application | Saved revisioned drafts, editable review, explicit approval gate |

Some defaults deserve product-specific verification: country of origin (`China`/`CN`), TARIC codes, tax class/rate, care instructions, manufacturing and connector codes. They are preserved as editable supplied defaults, not inferred from photographs. The source enum spelling `lilla` is retained exactly because changing a connector value without the target schema would be speculative.

## Migration choices

- **Local application:** Python standard-library HTTP service, SQLite persistence, vanilla browser interface. No n8n, PostgreSQL, Node build or runtime package installation is required.
- **OpenAI:** selected by the user during implementation. Image analysis and language generation use the Responses API with Structured Outputs. No legacy Google credentials or Telegram integration are used.
- **Batch by language:** analyze all uploaded images together, then generate applicable attributes/master/platform content once per requested language. This removes the old per-attribute request loop.
- **Review snapshots:** drafts preserve the category rules and platform constraints used to produce them. Editing input intentionally rebuilds the snapshot; editing final content revalidates and revokes approval.
- **Explicit mappings:** the existing Maytree activation flag is provisionally associated with Shopify; the Intergate Consulting & Trade channel is provisionally associated with OTTO based on the surrounding workflow. These assignments are editable and need confirmation against the Wawi installation. Channel IDs/names are retained from the source.
- **Language exports and positional stability:** the user confirmed JTL-Wawi 2.0.3 and position-based mapping. Attribute files have exactly `Artikelnummer, Artikelname, Attributgruppe, Attributname, Attributwert`. Other files start with Artikelnummer and Artikelname. A persisted append-only column registry prevents movement across category/platform/language selections or configuration edits. Separate language filenames avoid adding language columns to the five-column attribute contract.
- **SKU/EAN:** preserve the daily SKU format using a local transactional counter. User-entered existing SKUs are supported. External EAN pool allocation is deferred until a real source/connection is supplied; valid user-entered GTINs are supported now.
- **Phase boundaries:** phase one only creates files. Direct Ameise execution and additional AI product-image generation remain phase two.

## Information still needed for installation-specific import verification

The user confirmed version **2.0.3**, the exact five-column attribute structure, and that flexible non-attribute columns can be mapped once as long as their positions remain fixed. The implementation now follows that contract. Remaining installation-specific verification is:

1. Test one approved bundle using the user's positional Ameise templates, including language/platform selection and handling of blank unused columns.
2. Confirm the inferred mappings between Wawi sales-channel names and platform IDs, including functional versus translatable attributes and connector-specific language identifiers.
3. Confirm price gross/net semantics, expected weight units, and the SKU/EAN source if multiple systems create products concurrently.

The implementation preserves a reviewable local export contract while these installation details remain unresolved. It does not claim a verified Wawi import without them.
