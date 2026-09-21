# AI-Powered PowerPoint Template Populator

## 1. Purpose

This project creates one grounded PowerPoint presentation from one or more uploaded PDF, DOCX, or TXT documents. It preserves the selected PowerPoint template's visual design while replacing approved text targets with generated content.

The application supports:

- Streamlit for interactive use.
- FastAPI for programmatic integration.
- Elasticsearch for template metadata, structure, and hybrid retrieval.
- MinIO for template binaries and uploaded source documents.
- IBM watsonx.ai for embeddings, template reranking, and grounded content generation.
- `python-pptx` for template parsing and presentation population.

## 2. System Architecture

```text
Source documents
    |
    v
Validation and extraction
    |
    v
Unified factual source and slide plan
    |
    +---------------- Manual template selection
    |
    +---------------- Automatic hybrid retrieval
                         |
                         +-- watsonx embedding
                         +-- Elasticsearch BM25 + vector search
                         +-- watsonx candidate reranking
    |
    v
Retrieve PPTX from MinIO and verify SHA-256
    |
    v
Generate semantic slide content with watsonx.ai
    |
    v
Normalize, validate, and perform one focused repair when necessary
    |
    v
Map semantic content to authoritative template targets in Python
    |
    v
Populate and validate PPTX
    |
    v
Streamlit/FastAPI download
```

### Storage responsibilities

| Component | Stores |
|---|---|
| Elasticsearch | Template identity, descriptions, slide/shape metadata, writable targets, embedding, checksum, and MinIO object reference |
| MinIO templates bucket | Original template PPTX files |
| MinIO uploads bucket | Valid uploaded source documents |
| Local `generated/` | UUID-named generated presentations |

PPTX binary data is not stored in Elasticsearch.

## 3. Repository Layout

```text
ppt_template_populator/
|-- app.py                         Streamlit frontend
|-- api.py                         FastAPI application
|-- config.py                      Environment-backed settings
|-- requirements.txt
|-- README.md
|-- PROJECT_DOCUMENTATION.md
|-- scripts/
|   |-- ingest_template.py         Template ingestion/update command
|   |-- replace_template_catalogue.py
|   `-- verify_ingested_templates.py
|-- src/
|   |-- application_service.py     Shared API-facing business service
|   |-- document_extractor.py      PDF/DOCX/TXT/RTF/DOC extraction
|   |-- source_batch.py            Multi-file validation and combination
|   |-- elastic_client.py          Elasticsearch connection and mapping
|   |-- template_indexer.py        Template document writes
|   |-- template_retriever.py      Listing, retrieval, and hybrid search
|   |-- minio_client.py            Object storage operations
|   |-- embedding_client.py        watsonx embedding interface
|   |-- watsonx_client.py          Supported watsonx chat interface
|   |-- model_discovery.py         Available model discovery
|   |-- model_evaluator.py         Optional candidate benchmarking
|   |-- template_parser.py         Recursive PPTX structure extraction
|   |-- target_metadata.py         Canonical writable-target rules
|   |-- slide_planner.py           Ordered semantic slide plan
|   |-- prompt_builder.py          Grounded generation and repair prompts
|   |-- response_models.py         Strict Pydantic response models
|   |-- content_normalizer.py      Bullet and content normalization
|   |-- response_validator.py      IDs, schema, limits, and grounding checks
|   |-- semantic_content.py        Semantic-to-template mapping
|   |-- ppt_populator.py           Formatting-aware PPTX population
|   |-- visual_validator.py        Post-population structural checks
|   |-- pipeline.py                End-to-end orchestration
|   `-- security.py                Upload/path/secret safety helpers
|-- templates/                     Local source templates for ingestion
|-- generated/                     Generated presentations
`-- tests/                         Offline mocked tests
```

## 4. Prerequisites

- Python 3.11 or newer
- Elasticsearch 8.x
- MinIO or compatible S3 storage
- IBM watsonx.ai project and API key
- Network access from the backend to those services

## 5. Installation

Run these commands from `D:\ppt_temp`:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\ppt_template_populator\requirements.txt
```

If PowerShell blocks activation, commands can use the virtual-environment Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\ppt_template_populator\requirements.txt
```

## 6. Configuration

Create `ppt_template_populator/.env` from `.env.example`. Never commit or print this file.

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

Important distinctions:

- Elasticsearch uses its API port, normally `9200`.
- Kibana is a separate interface and must not be used as `ELASTICSEARCH_URL`.
- `MINIO_ENDPOINT` is `host:port`, without `http://` or `https://`.
- `MINIO_SECURE` selects HTTP or HTTPS.
- Model IDs must be available to the configured watsonx project and region.

## 7. Starting the Applications

### Streamlit

From `D:\ppt_temp`:

```powershell
.\.venv\Scripts\python.exe -m streamlit run .\ppt_template_populator\app.py
```

Open `http://localhost:8501`.

### FastAPI

From `D:\ppt_temp`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn api:app --app-dir .\ppt_template_populator --host 0.0.0.0 --port 8000
```

Available URLs:

- API base: `http://localhost:8000`
- Swagger UI: `http://localhost:8000/docs`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

Use one Uvicorn worker because the prototype job store is process-local.

## 8. FastAPI Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Safe service and configuration status |
| `POST` | `/api/v1/documents/extract` | Validate and extract an uploaded source document |
| `GET` | `/api/v1/templates` | List indexed template summaries |
| `POST` | `/api/v1/templates/select` | Retrieve and rerank candidate templates |
| `POST` | `/api/v1/presentations` | Create an asynchronous generation job |
| `GET` | `/api/v1/jobs/{job_id}` | Read job state, stages, warnings, and result URL |
| `GET` | `/api/v1/jobs/{job_id}/download` | Download the completed PPTX |

Example generation request:

```json
{
  "topic": "Women Empowerment in India",
  "source_content": "Extracted factual source content...",
  "audience": "Senior leadership",
  "tone": "Formal",
  "user_instructions": "Focus on measurable outcomes.",
  "selection_mode": "automatic"
}
```

For manual selection, use `"selection_mode": "manual"` and provide `template_id`.

## 9. Template Ingestion

Template ingestion is an administrator operation and is not exposed through Streamlit.

From `ppt_template_populator`:

```powershell
python scripts\ingest_template.py templates\Professional_PPT_Template.pptx `
  --name "Professional PPT Template" `
  --description "Professional template for reports and proposals" `
  --category "Business" `
  --use-cases "business strategy,management report,project proposal" `
  --audiences "executives,managers,clients" `
  --tones "professional,formal,data-driven" `
  --visual-style "clean corporate structured" `
  --supported-sections "title,introduction,problem statement,objectives,solution,workflow,evidence,conclusion,thank you"
```

Use `--update-existing` to reparse a checksum-identical existing template while preserving its `template_id` and object reference:

```powershell
python scripts\ingest_template.py templates\Professional_PPT_Template.pptx `
  --name "Professional PPT Template" `
  --update-existing
```

Ingestion performs extension, size, ZIP structure, PowerPoint readability, checksum, target, embedding, MinIO, and Elasticsearch validation. If an upload succeeds but indexing fails, the new object is removed.

## 10. Template Target Model

The parser recursively inspects ordinary and grouped shapes.

Two stable target kinds are supported:

```json
{"target_kind": "placeholder", "target_id": 0}
```

For native placeholders, `target_id` is `placeholder_format.idx`.

```json
{"target_kind": "shape", "target_id": 12}
```

For Canva and ordinary text shapes, `target_id` is `shape.shape_id`.

Target metadata includes role, original text, geometry, font information, margins, capacity, replaceability, requirement status, and text category. Branding, legal text, page numbers, footers, and decorative content are protected. Editable samples and visual instructions are cleared even when no generated content is assigned to them.

## 11. Generation Pipeline

1. Validate every uploaded file independently.
2. Reject empty, oversized, unsupported, or checksum-duplicate inputs safely.
3. Extract readable content while retaining source boundaries.
4. Deduplicate repeated paragraphs across valid sources.
5. Build one unified factual source and extractive summary.
6. Create the ordered slide plan.
7. Select a template manually or automatically.
8. Retrieve its PPTX object from MinIO.
9. Verify the object checksum before parsing.
10. Generate semantic content one slide at a time.
11. Validate slide number, section type, title, bullets, and speaker notes.
12. Perform at most one controlled repair for malformed content.
13. Map semantic titles and bullets to trusted template targets in Python.
14. Clear approved sample/instruction targets before writing.
15. Write each bullet as one PowerPoint paragraph.
16. Run overflow, bounds, collision, placeholder, and mandatory-section checks.
17. Save a UUID-named PPTX and expose it for download.

The model never chooses shape or placeholder IDs. It generates semantic content only; Python owns technical target mapping.

## 12. Content and Validation Rules

- Uploaded documents are the factual source of truth.
- User instructions guide presentation style and emphasis but are not evidence.
- Unknown slides and targets are rejected.
- Duplicate target assignments are rejected.
- Required content must exist before population.
- Bullet arrays are normalized without character-by-character splitting.
- Wrapped continuation lines may be reconstructed only when multiple signals agree.
- Standalone list markers and isolated body characters are rejected.
- Meaningful lowercase bullets are safely capitalized unless they use intentional technical forms such as `pH`, `iOS`, or `e-commerce`.
- Capacity validation uses target geometry and font information.
- Minor fit adjustments preserve complete ideas; important content is not silently truncated.

## 13. Security

- Credentials are loaded with `python-dotenv` and Pydantic settings.
- Secrets are represented with `SecretStr` where applicable.
- Credentials, tokens, request headers, embeddings, and binary content are not logged.
- `.env` is ignored by Git.
- Filenames are sanitized and paths are confined to approved directories.
- Upload size is checked per file and per batch.
- PPTX files are validated as ZIP-based Office documents.
- Output filenames use UUIDs.
- Checksums are verified before template population.
- No `eval`, pickle, executable upload handling, or macro execution is used.
- User-facing errors omit internal stack traces and sensitive payloads.

## 14. Testing

From `D:\ppt_temp`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The repository-level `pytest.ini` restricts collection to the actual project tests and avoids generated/cache directories.

Tests mock watsonx.ai, Elasticsearch, and MinIO. They do not make paid model calls or require live infrastructure.

Current coverage includes:

- Document extraction and multi-file batching
- Upload and path security
- Template parsing and Canva targets
- Elasticsearch and MinIO failure handling
- Embedding configuration and dimension consistency
- Manual and automatic template selection
- Semantic content validation and controlled repair
- Bullet reconstruction and false-fragment protection
- Slide ordering and target mapping
- Formatting-preserving PowerPoint population
- Overflow and visual-instruction checks
- FastAPI endpoints and job lifecycle

## 15. Troubleshooting

### Elasticsearch did not respond

- Confirm the configured address is the Elasticsearch API, not Kibana.
- Test `http://<host>:9200` from the backend machine.
- A timeout, authentication rejection, and connection refusal are reported separately.

### watsonx embedding generation failed

- Use the configuration diagnostic in Streamlit.
- Confirm the embedding model is available to the configured project and region.
- Re-ingest templates if the embedding model or dimension changes.
- Never mix query and template embeddings from different models.

### Template checksum mismatch

The MinIO object differs from the indexed checksum. Do not generate from it. Re-ingest the trusted original template.

### No compatible template

Verify that templates are indexed, contain writable targets, use the configured embedding model and dimension, and are marked safe for automatic population.

### Schema validation failed

Review sanitized developer details. The pipeline permits one focused repair but rejects unknown IDs, duplicate mappings, unsafe nesting, and invalid mandatory content.

### PowerPoint text or layout limitations

`python-pptx` does not render slides. Fit and collision checks are geometry-based approximations. SmartArt, animations, embedded objects, and advanced mixed-run formatting have limited editing support.

### Pytest reports access denied during collection

Run from `D:\ppt_temp` using the repository command above. The root `pytest.ini` limits collection to `ppt_template_populator/tests`.

## 16. Operational Checklist

Before generation:

1. Confirm Elasticsearch, MinIO, and watsonx status.
2. Confirm at least one compatible template is indexed.
3. Verify template and query embeddings use the same model and dimensions.
4. Upload related source files and review extraction results.
5. Choose manual or automatic selection.

After generation:

1. Review warnings and repaired sections.
2. Confirm title, introduction, and closing slides are present.
3. Open the downloaded PPTX in PowerPoint or LibreOffice.
4. Visually inspect overflow, alignment, imagery, and brand preservation.
5. Treat rendering inspection as the final quality gate for production delivery.

