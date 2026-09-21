# AI-Powered PowerPoint Template Populator

For the consolidated architecture, setup, API, ingestion, security, testing, and operations guide, see [PROJECT_DOCUMENTATION.md](PROJECT_DOCUMENTATION.md).

This application turns one or more uploaded PDF, DOCX, or TXT source documents into one populated PowerPoint. Administrators ingest templates ahead of time. The backend stores template structure, semantic profile, embedding, and checksum in Elasticsearch, while the actual PPTX is stored in MinIO. Streamlit remains the interactive frontend and FastAPI exposes the same service pipeline for programmatic clients.

## Architecture and ownership

```text
Administrator CLI
  -> validate/parse PPTX
  -> SHA-256 checksum
  -> watsonx embedding of template profile
  -> MinIO: store the PPTX object (ppt-templates bucket)
  -> Elasticsearch: metadata + nested structure + vector + MinIO object reference (no binary)

Streamlit user upload
  -> user uploads one or more PDF/DOCX/TXT sources for one presentation
  -> each file is validated and extracted independently; duplicate paragraphs are removed
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

FastAPI client
  -> document extraction or grounded JSON generation request
  -> same service factory, retrieval, generation, validation and population modules
  -> background job ID -> status polling -> confined PPTX download
```

- **Streamlit** collects the uploaded document and optional user instructions, shows an extraction preview, displays safe pipeline/selection evidence, and returns the final download.
- **FastAPI** exposes typed health, extraction, template, generation-job and download endpoints. Swagger documentation is generated at `/docs`.
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
MAX_TOTAL_UPLOAD_MB=100
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

The user uploads one or more related source documents. A single-file upload remains fully supported. The backend:

1. Extracts text and a title from the document (DOCX/PDF/TXT/MD/RTF natively; legacy DOC via best-effort text recovery).
2. Archives each valid original upload separately to the `ppt-uploads` MinIO bucket.
3. Derives `topic` (document title), `source_content` (extracted text), and sensible defaults for audience/tone/slide count (a word-count heuristic, ~120 words per slide, bounded 6–20).
4. **Automatic mode:** embeds the derived requirements, runs BM25 + cosine similarity against indexed templates (metadata only, no binary), scores candidates, and lets watsonx Granite make the final selection (one repair allowed; invalid repair falls back to the highest-scoring candidate). **Manual mode:** uses the template the user picked directly, skipping retrieval and AI selection.
5. Fetches the selected template's PPTX object from MinIO and verifies its SHA-256 checksum before opening it from a securely named temporary file.
6. Generates content in bounded chunks, validates every `(target_kind, target_id)` against the selected slide plus approximate character limits, and runs focused recovery for anything missing — falling back to placeholder content only as a last resort.
7. Populates the template, saves a UUID-named result, and deletes the temporary source in a `finally` path.

Low selection confidence (automatic mode) produces a warning. No candidates, no usable templates, or incompatible embedding configurations stop safely.

### Grounding and output accuracy

The uploaded document is the only factual source of truth. The prompt keeps `factual_source_document` separate from `generation_guidance`; optional User Instructions guide tone, audience, focus, exclusions and design, but never supply facts. watsonx.ai is instructed not to invent facts, figures, names, dates, quotations or sources. Template selection uses a bounded grounded excerpt to control context size, while final slide generation receives the complete extracted source. Every chunk includes the current Pydantic JSON schema, trusted slide roles and exact character limits. Generation uses low temperature, strict validation, one controlled repair, focused target recovery and explicit fallback warnings.

## Start the application

```powershell
streamlit run app.py
```

The main screen shows a document uploader, optional **User Instructions**, and a template-selection toggle (Automatic / Manual). There is still no way to ingest templates from the UI — that remains an administrator-only CLI action.

## FastAPI backend

Start the REST service from the project directory:

```powershell
python -m uvicorn api:app --host 0.0.0.0 --port 8000
```

Open Swagger documentation at `http://localhost:8000/docs` or OpenAPI JSON at `http://localhost:8000/openapi.json`.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Safe Elasticsearch, MinIO and watsonx configuration status |
| `POST` | `/api/v1/documents/extract` | Validate and extract an uploaded document |
| `GET` | `/api/v1/templates` | List safe indexed template summaries |
| `POST` | `/api/v1/templates/select` | Run grounded hybrid retrieval and template reranking |
| `POST` | `/api/v1/presentations` | Queue automatic or manual presentation generation |
| `GET` | `/api/v1/jobs/{job_id}` | Read job status, stages, warnings and download URL |
| `GET` | `/api/v1/jobs/{job_id}/download` | Download a completed PPTX from the confined output directory |

`POST /api/v1/presentations` accepts JSON. Set `selection_mode` to `automatic`, or use `manual` with a non-empty `template_id`. `source_content` is factual source data; `user_instructions` is guidance only. The prototype job store is process-local, so use one Uvicorn worker. A production multi-worker deployment should replace `InMemoryJobStore` with a shared queue/database while keeping the endpoint models unchanged.

```json
{
  "topic": "Women Empowerment in India",
  "source_content": "Extracted factual document content...",
  "audience": "Senior leadership",
  "tone": "Formal",
  "user_instructions": "Focus on measurable outcomes and exclude unrelated implementation detail.",
  "selection_mode": "automatic"
}
```

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
Generated presentations default to a 14-slide, single-purpose research narrative:
title, agenda/introduction, problem, research gap, objectives, inputs, methodology,
solution, analysis, findings, limitations, validation, novelty, and conclusion/Q&A.
Requested counts are supported by retaining a related subset without combining
unrelated purposes. The planner maps roles onto writable layouts, reuses compatible
layouts when necessary, and keeps placeholder and Canva shape IDs under strict
validation.

The layout engine derives a proportional style profile from each selected template:
aspect ratio, safe margins, title/body hierarchy, readable font sizes, card gaps,
footer zone, and column capacity. It preserves the template font family, theme,
colors, branding, and backgrounds. Re-ingest templates after upgrading to capture
the new slide-dimension and font-family metadata; existing metadata uses conservative
fallbacks.

PDF extraction removes repeated page-edge headers and footers without silently
truncating the extracted text. The planner distributes the complete source
across grounded slide excerpts; user instructions affect presentation guidance
only and are never treated as factual evidence.
