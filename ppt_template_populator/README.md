# AI-Powered PowerPoint Template Populator

This application turns an uploaded document (DOCX, PDF, TXT, MD, RTF, or legacy DOC) into a populated PowerPoint. Administrators ingest templates ahead of time. The backend stores template structure, semantic profile, embedding, and checksum in Elasticsearch, while the actual PPTX (and every user-uploaded source document) is stored as an object in MinIO. At generation time the user only uploads a document — topic, audience, tone, and slide count are all derived automatically — and either lets a watsonx.ai chat model pick the best template, or picks one manually from a list.

## Architecture and ownership

```text
Administrator CLI
  -> validate/parse PPTX
  -> SHA-256 checksum
  -> watsonx embedding of template profile
  -> MinIO: store the PPTX object (ppt-templates bucket)
  -> Elasticsearch: metadata + nested structure + vector + MinIO object reference (no binary)

Streamlit user upload
  -> user uploads one document (DOCX/PDF/TXT/MD/RTF/DOC) — nothing else to fill in
  -> text + title extracted locally (python-docx / pypdf / striprtf / plain text)
  -> original upload archived to MinIO (ppt-uploads bucket) for traceability
  -> topic/audience/tone/slide count derived automatically from the document

Automatic mode                              Manual mode
  -> retrieval query -> watsonx embedding      -> user picks a template from a dropdown
  -> Elasticsearch BM25 + cosine search           (skips retrieval + AI selection)
     (top 5, metadata only, no binary)
  -> watsonx Granite final template selection
                    \                          /
                     -> selected template's MinIO object key
  -> MinIO: fetch the PPTX object + SHA-256 verification + temporary PPTX
  -> watsonx Granite structured slide content (chunked, with bounded focused recovery)
  -> Pydantic validation/one repair; any target still missing after recovery gets
     safe placeholder content instead of failing the whole presentation
  -> python-pptx formatting-aware population
  -> UUID output download -> temporary source cleanup
```

- **Streamlit** collects the uploaded document, shows an extraction preview, displays safe pipeline/selection evidence, and returns the final download. It never receives raw PPTX bytes, embeddings, or credentials directly — those stay in the backend/MinIO layer.
- **Python backend** owns model calls, document text extraction, checksums, temporary files, response validation, and orchestration.
- **Elasticsearch** stores catalog metadata, slide structure, dense vectors, and a `minio_bucket`/`minio_object_key` reference — never the PPTX binary itself. Candidate responses additionally exclude `template_embedding`.
- **MinIO** stores the actual PPTX objects (`ppt-templates` bucket) and archived user uploads (`ppt-uploads` bucket) as S3-compatible object storage.
- **Embedding model** represents template profiles and the uploaded document's content in the same vector space. Mixed models and dimension mismatches are rejected.
- **Granite/chat model** chooses among retrieved candidates (automatic mode only), then separately generates slide content. Binary data is never sent to a model.
- **python-pptx** parses standard placeholders using `placeholder_format.idx`, recursively discovers approved Canva-style text shapes using `shape_id`, and edits a decoded copy while retaining unrelated package content.

## Environment configuration

Python 3.11 or newer, Elasticsearch 8.x, a MinIO (or any S3-compatible) server, and an IBM watsonx.ai project are required. Copy `.env.example` to `.env`; `.env` is ignored by Git.

```dotenv
WATSONX_API_KEY=
WATSONX_URL=https://us-south.ml.cloud.ibm.com
WATSONX_PROJECT_ID=
WATSONX_MODEL_ID=
WATSONX_EMBEDDING_MODEL_ID=
ELASTICSEARCH_URL=http://localhost:9200
ELASTICSEARCH_USERNAME=
ELASTICSEARCH_PASSWORD=
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=
MINIO_SECRET_KEY=
MINIO_SECURE=false
MINIO_TEMPLATES_BUCKET=ppt-templates
MINIO_UPLOADS_BUCKET=ppt-uploads
MAX_UPLOAD_MB=25
MAX_REQUIRED_TARGETS_PER_CHUNK=12
REQUEST_TIMEOUT_SECONDS=120
TEMPLATE_SELECTION_CONFIDENCE=0.60
```

Use the Elasticsearch API endpoint on port `9200`; Kibana on port `5602` is a separate UI. `MINIO_ENDPOINT` is host:port without a scheme (`MINIO_SECURE` controls http vs https). `WATSONX_MODEL_ID` and `WATSONX_EMBEDDING_MODEL_ID` are fallbacks obtained from model IDs shown as available in the configured watsonx project/region. Discovery prefers Granite 4 H Small for chat and Granite Embedding 278M Multilingual for embeddings when those models are actually available.

Install dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Administrator ingestion

Users cannot ingest templates through Streamlit. An administrator runs:

```powershell
python scripts/ingest_template.py templates/startup_pitch.pptx `
  --name "Startup Pitch Deck" `
  --description "Modern investor presentation for startup fundraising" `
  --category "Business" `
  --use-cases "startup pitch,investor pitch,fundraising" `
  --audiences "investors,business leaders" `
  --tones "professional,persuasive,modern" `
  --visual-style "minimal and data-focused" `
  --supported-sections "problem,solution,market,business model,financials"
```

The command validates size, extension, ZIP structure, and PowerPoint readability; sanitizes the filename; recursively extracts layouts, grouped shapes, stable placeholder IDs, ordinary text-shape IDs, text capacity, and image/chart support; calculates SHA-256; creates a detailed profile and embedding; uploads the PPTX object to the `ppt-templates` MinIO bucket; rejects duplicate IDs/checksums; and stores an Elasticsearch document that references the MinIO object (never the binary itself). Its console summary excludes binary data and credentials.

For a template ingested before Canva target support, rerun the same command with `--update-existing`. Updating is allowed only when the checksum matches an existing document, and it preserves that document's `template_id`.

## Standard PowerPoint and Canva targets

Every parsed slide stores complete recursive shape metadata and a separate approved `targets` list. Standard text placeholders use `target_kind: "placeholder"` and their `placeholder_format.idx` as `target_id`. Meaningful ordinary Canva text boxes use `target_kind: "shape"` and their `shape_id` as `target_id`.

Classification uses placeholder type, existing text, font size, position, and dimensions to assign `title`, `subtitle`, `heading`, `body`, `caption`, `footer`, `page number`, `decorative`, or `unknown`. Empty ordinary shapes are not writable. Decorative/logo/footer/page-number shapes are protected unless their shape name explicitly contains `replaceable` or begins with `content_`/`content `.

Ingestion records usable placeholder count, usable ordinary text-target count, slides without writable targets, and `safe_for_automatic_population`. Hybrid retrieval filters on that safety flag, so templates without usable targets cannot be automatically selected.

If content generation still can't produce a required target after focused, bounded retries (a template with an unusually large number of required table/shape targets), the pipeline no longer fails the whole presentation: it inserts safe placeholder text (the shape's original text, or a short generic label) for that one target and reports it in the warnings list so it can be reviewed and edited after download.

## Elasticsearch mapping and migration

The `ppt_templates` index uses explicit field types, nested slides/shapes, an indexed `dense_vector` with cosine similarity, and `minio_bucket`/`minio_object_key` keyword fields (no binary field). Vector dimensions are fixed when the first template creates the index.

An index made by an older project version (which stored a `pptx_binary` field) is incompatible. The application reports `MappingMigrationRequired` and does not delete or alter data. Reindex the old catalog under a different name, upload each document's decoded `pptx_binary` to MinIO, add the `minio_bucket`/`minio_object_key` fields, explicitly remove the old index only after verifying the backup, then rerun ingestion so the new index is created with the embedding dimensions returned by watsonx.ai. Do not attempt to update a `dense_vector` dimension in place.

## Generation workflow

The user uploads exactly one document — nothing else to fill in. The backend:

1. Extracts text and a title from the document (DOCX/PDF/TXT/MD/RTF natively; legacy DOC via best-effort text recovery).
2. Archives the original upload to the `ppt-uploads` MinIO bucket.
3. Derives `topic` (document title), `source_content` (extracted text), and sensible defaults for audience/tone/slide count (a word-count heuristic, ~120 words per slide, bounded 6–20).
4. **Automatic mode:** embeds the derived requirements, runs BM25 + cosine similarity against indexed templates (metadata only, no binary), scores candidates, and lets watsonx Granite make the final selection (one repair allowed; invalid repair falls back to the highest-scoring candidate). **Manual mode:** uses the template the user picked directly, skipping retrieval and AI selection.
5. Fetches the selected template's PPTX object from MinIO and verifies its SHA-256 checksum before opening it from a securely named temporary file.
6. Generates content in bounded chunks, validates every `(target_kind, target_id)` against the selected slide plus approximate character limits, and runs focused recovery for anything missing — falling back to placeholder content only as a last resort.
7. Populates the template, saves a UUID-named result, and deletes the temporary source in a `finally` path.

Low selection confidence (automatic mode) produces a warning. No candidates, no usable templates, or incompatible embedding configurations stop safely.

## Start the application

```powershell
streamlit run app.py
```

The main screen shows a document uploader and a template-selection toggle (Automatic / Manual). There is still no way to ingest templates from the UI — that remains an administrator-only CLI action.

## Verification

Kibana (`http://<host>:5602`) for the search index — never request `pptx_binary`, it no longer exists:

```http
GET ppt_templates/_count
```

```http
GET ppt_templates/_search
{
  "_source": { "excludes": ["template_embedding"] },
  "query": { "match_all": {} }
}
```

MinIO console (`http://<host>:9001` by default) for stored objects — the `ppt-templates` bucket should contain one `.pptx` object per ingested template, and `ppt-uploads` should contain archived user uploads.

## Tests

All tests use synthetic PowerPoints/documents and mocked Elasticsearch/MinIO/watsonx services; they make no paid calls and touch no real server.

```powershell
python -m pytest -q
```

Coverage includes ingestion, invalid PPTX rejection, filename/path security, document text extraction (DOCX/TXT and error cases), checksum mismatch, duplicate prevention, profile/embedding dimensions, mapping compatibility, mixed-model prevention, hybrid retrieval, MinIO object retrieval and its failure modes, selection validation/fallback/low confidence, temporary cleanup, slide validation, population, the complete automatic pipeline, focused-recovery fallback to placeholder content, service errors, and missing configuration.

## Troubleshooting

- **Elasticsearch connection refused:** confirm Elasticsearch is running on the configured `9200` API port and firewall/routing permits access. Do not use Kibana port `5602`.
- **MinIO connection refused:** confirm the MinIO server is reachable at `MINIO_ENDPOINT` and that `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY` are correct.
- **Timeout:** verify network reachability and `REQUEST_TIMEOUT_SECONDS`.
- **Authentication rejected:** verify the Elasticsearch/MinIO credentials and privileges without printing them.
- **Migration required:** preserve/reindex the old index, then explicitly replace it as described above.
- **No compatible embedding model:** configure a project-available embedding model ID and ingest templates with it.
- **No candidates / no templates to choose from:** ingest compatible templates first. Generation will not proceed with an invalid template.
- **Checksum mismatch:** treat the stored MinIO object as corrupted or altered and reingest from the trusted source.
- **Unsupported file type / could not extract text:** re-save the document as `.docx` for the most reliable extraction; legacy `.doc` extraction is best-effort only.
- **Stale settings:** restart Streamlit because configuration and Elasticsearch/MinIO service clients are process-cached.

Use **Test watsonx configuration** in System status to discover embedding models for the configured project/region, validate the configured model ID, and determine its output dimensions.

## Security and limitations

Credentials are loaded from `.env` into backend-only settings, secret values use `SecretStr`, errors are masked, and neither credentials nor binary data are logged. Checksums are verified before parsing, paths/filenames are sanitized, output names use UUIDs, and no `eval`, pickle, or executable upload handling is used. Uploaded documents are validated for size before extraction and only text is extracted — no macros or embedded objects from the uploaded document are ever executed.

`python-pptx` has no PowerPoint rendering engine, so text-fit estimates are approximate. Replacement preserves the first available paragraph/run formatting where possible, but complex mixed-run styling cannot always map to new prose. SmartArt, animations, embedded objects, advanced effects, and some chart internals have limited editing support; untouched XML content is generally retained because the presentation is edited rather than rebuilt. Legacy `.doc` extraction uses a best-effort printable-text scrape (there is no reliable pure-Python binary-DOC parser) — re-saving as `.docx` is recommended when fidelity matters.
