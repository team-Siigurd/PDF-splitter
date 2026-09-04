# Siigurd PDF Splitter

En gratis, selvstændig Render-service, der henter en PDF direkte fra en kortlivet
OneDrive-download-URL og opdeler den i gyldige PDF-filer på højst 45.000.000
bytes. Servicen er adskilt fra den eksisterende lydsplitter.

Servicen bruger kun Python-standardbiblioteket og det open-source værktøj
`qpdf`. Docker-imaget installerer selv `qpdf`.

## Hvad servicen kontrollerer

- Den accepterer kun HTTPS-downloads fra tilladte Microsoft/OneDrive-domæner.
- Den streamer filen til disk og indlæser derfor ikke hele PDF'en i RAM.
- Den kontrollerer den forventede filstørrelse.
- Den kan kontrollere OneDrives SHA-1-checksum.
- Den kontrollerer, at filen starter som en PDF.
- Den validerer hver output-PDF med `qpdf --check`.
- Den kontrollerer, at alle sider er med præcis én gang og i rigtig rækkefølge.
- Den kontrollerer, at ingen del overstiger den ønskede filstørrelse.
- Den tillader som standard kun ét PDF-job ad gangen.
- Midlertidige filer udløber automatisk efter én time.

## Filer i projektet

- `app.py`: Selve API'et og PDF-opdelingen.
- `Dockerfile`: Installerer Python og qpdf på Render.
- `render.yaml`: Gratis Render-konfiguration.
- `.dockerignore` og `.gitignore`: Forhindrer unødvendige filer i deployment.

## 1. Opret repository på GitHub

1. Pak ZIP-filen ud på din computer.
2. Log ind på GitHub.
3. Vælg **New repository**.
4. Kald det eksempelvis `siigurd-pdf-splitter`.
5. Vælg gerne **Private**.
6. Opret repositoryet uden at tilføje en anden README eller `.gitignore`.
7. Vælg **Add file > Upload files**.
8. Upload alle filerne fra den udpakkede mappe. Upload selve filerne, så
   `Dockerfile`, `app.py` og `render.yaml` ligger direkte i repositoryets rod.
9. Commit filerne til `main`.

## 2. Opret en separat service på samme Render-konto

Du skal ikke oprette en ny Render-konto. Den eksisterende lydservice forbliver
urørt.

### Anbefalet: opret via Blueprint

1. Log ind på den eksisterende Render-konto.
2. Vælg **New > Blueprint**.
3. Forbind det nye GitHub-repository.
4. Render finder automatisk `render.yaml`.
5. Når Render spørger efter `SPLITTER_API_KEY`, indsætter du en lang, tilfældig
   hemmelig nøgle. Brug mindst 32 tilfældige tegn. Gem den i en password manager;
   den samme værdi skal senere bruges i Make.
6. Bekræft oprettelsen.

`render.yaml` vælger automatisk:

- Service: Web Service
- Runtime: Docker
- Plan: Free
- Region: Frankfurt
- Health check: `/health`
- Maksimalt ét samtidigt PDF-job
- Automatisk deploy ved nye commits

Hvis navnet `siigurd-pdf-splitter` allerede er taget, giver Render servicen et
andet hostname. Koden bruger automatisk Render-hostnavnet.

### Alternativ: opret som almindelig Web Service

Hvis du ikke vil bruge Blueprint:

1. Vælg **New > Web Service**.
2. Vælg det nye GitHub-repository.
3. Name: `siigurd-pdf-splitter`.
4. Region: Frankfurt.
5. Branch: `main`.
6. Runtime/Language: Docker.
7. Instance type/plan: Free.
8. Health Check Path: `/health`.
9. Tilføj environment variables fra tabellen nedenfor.
10. Opret servicen.

| Environment variable | Værdi |
|---|---:|
| `SPLITTER_API_KEY` | En hemmelig tilfældig værdi på mindst 32 tegn |
| `MAX_SOURCE_BYTES` | `500000000` |
| `DEFAULT_MAX_PART_BYTES` | `45000000` |
| `FILE_TTL_SECONDS` | `3600` |
| `MAX_CONCURRENT_JOBS` | `1` |
| `ALLOWED_DOWNLOAD_HOST_SUFFIXES` | `.sharepoint.com,.sharepointonline.com,.1drv.com,.onedrive.com,.microsoft.com,.storage.live.com` |

`SPLITTER_API_KEY` skal markeres som secret. Den må aldrig lægges i GitHub.

## 3. Kontrollér deployment

Vent, indtil deploy-loggen viser, at servicen lytter på porten fra Render.
Besøg derefter:

```text
https://DIT-RENDER-HOSTNAVN.onrender.com/health
```

Et korrekt svar ligner:

```json
{
  "status": "ok",
  "version": "1.0.0",
  "qpdf": "qpdf version ..."
}
```

Første kald kan tage omkring et minut, fordi en gratis Render-service går i
dvale efter inaktivitet.

## 4. Hent en kortlivet OneDrive-download-URL i Make

Tilføj modulet **OneDrive > Make an API Call** umiddelbart før Make starter
PDF-jobbet. Det sikrer, at URL'en stadig er gyldig, når Render bruger den.

Indstil modulet sådan:

```text
Method: GET
URL: /v1.0/drives/{{28.parentReference.driveId}}/items/{{28.id}}
```

Kør modulet én gang. Responsen skal indeholde:

```text
@microsoft.graph.downloadUrl
name
size
file > hashes > sha1Hash
```

Brug ikke `webUrl`. Det er browserlinket og kræver normalt Microsoft-login.

Hvis OneDrive-modulet allerede tilføjer `/v1.0` automatisk og afviser URL'en,
brug i stedet:

```text
/drives/{{28.parentReference.driveId}}/items/{{28.id}}
```

## 5. Start PDF-jobbet fra Make

Tilføj **HTTP > Make a request**.

```text
Method: POST
URL: https://DIT-RENDER-HOSTNAVN.onrender.com/jobs
Body type: Raw
Content type: application/json
Parse response: Yes
```

Headers:

```text
X-API-Key: den samme værdi som SPLITTER_API_KEY i Render
Content-Type: application/json
```

Body:

```json
{
  "download_url": "{{OneDrive API: @microsoft.graph.downloadUrl}}",
  "filename": "{{OneDrive API: name}}",
  "expected_size": {{OneDrive API: size}},
  "expected_sha1": "{{OneDrive API: file.hashes.sha1Hash}}",
  "max_part_size": 45000000
}
```

Vælg felterne fra Make-mappingpanelet; skriv ikke de viste pladsholdere
bogstaveligt.

Et korrekt svar har HTTP-status 202:

```json
{
  "job_id": "e4d3...",
  "status": "processing",
  "status_url": "https://...onrender.com/jobs/e4d3..."
}
```

Hvis servicen svarer med HTTP 429, behandler den allerede en anden PDF. Vent og
prøv igen. Det beskytter de 512 MB RAM på Free-planen.

## 6. Kontrollér jobstatus

Send et GET-kald til `status_url` med samme header:

```text
X-API-Key: SPLITTER_API_KEY-værdien
```

Mens jobbet arbejder:

```json
{
  "status": "processing",
  "stage": "downloading"
}
```

eller:

```json
{
  "status": "processing",
  "stage": "splitting"
}
```

Når jobbet er færdigt, indeholder svaret `parts[]`:

```json
{
  "status": "complete",
  "page_count": 160,
  "part_count": 8,
  "parts": [
    {
      "part": 1,
      "filename": "part_001_pages_1-20.pdf",
      "start_page": 1,
      "end_page": 20,
      "size": 41234567,
      "sha256": "...",
      "download_url": "https://...onrender.com/files/.../part_001_pages_1-20.pdf"
    }
  ]
}
```

## 7. Download og behandl delene i Make

1. Brug en Iterator på `parts[]`.
2. Brug et HTTP-modul til at downloade hver `download_url`.
3. Send headeren `X-API-Key` med ved download.
4. Upload den downloadede binære PDF-del til OpenAI.
5. Kør Responses API-kaldet med fil-ID'et fra dette nye uploadmodul.
6. Parse JSON-resultatet for hver del.
7. Brug en Array Aggregator til at samle alle highlights.
8. Opret først derefter det ene Airtable-record.

## 8. Slet midlertidige PDF-filer

Når Make har downloadet alle delene, send:

```text
DELETE https://DIT-RENDER-HOSTNAVN.onrender.com/jobs/{{job_id}}
X-API-Key: SPLITTER_API_KEY-værdien
```

Servicen sletter også automatisk afsluttede job efter `FILE_TTL_SECONDS`, som
som standard er 3600 sekunder.

## Vigtigt om polling i Make

Et enkelt Make-scenarie kan ikke på en elegant måde vente ubegrænset på et
asynkront job. Start derfor med at teste Render-servicen separat. Når testen
virker, kan Make-delen bygges på én af to måder:

1. Et startscenarie gemmer `job_id`, og et andet planlagt scenarie kontrollerer
   ventende job.
2. Render udvides med callback til en Make-webhook, som starter den videre
   behandling, når PDF'en er færdig.

Webhook-løsningen er normalt bedst, men den kræver, at de oplysninger, der skal
bruges senere i Airtable, enten sendes med som metadata eller gemmes midlertidigt.

## API-oversigt

| Metode | Endpoint | Kræver API-nøgle | Funktion |
|---|---|---|---|
| GET | `/health` | Nej | Kontrollerer service og qpdf |
| POST | `/jobs` | Ja | Starter download og opdeling |
| GET | `/jobs/{job_id}` | Ja | Returnerer status og outputdele |
| GET | `/files/{job_id}/{filename}` | Ja | Streamer en PDF-del |
| DELETE | `/jobs/{job_id}` | Ja | Sletter job og filer |

## Fejlsøgning

### `Download host is not allowed`

Se hostnavnet i fejlbeskeden. Hvis URL'en stadig tydeligt tilhører Microsoft,
kan domænesuffikset tilføjes til `ALLOWED_DOWNLOAD_HOST_SUFFIXES` i Render.
Tilføj aldrig vilkårlige domæner eller `*`.

### `Incomplete download`

Render modtog ikke samme antal bytes, som OneDrive oplyste. Start jobbet igen
med en ny `@microsoft.graph.downloadUrl`.

### `Downloaded file SHA-1 does not match OneDrive`

Downloaden matcher ikke originalen. Start jobbet igen. Fjern ikke kontrollen
for at tvinge filen igennem.

### `Page X alone is ... bytes`

En enkelt PDF-side er større end 45 MB. Dokumentet kan ikke løses ved almindelig
sideopdeling; den konkrete side skal komprimeres eller rasteriseres separat.

### Job forsvinder under behandling

Free-planen bruger et midlertidigt filsystem, og Render kan genstarte servicen.
Start jobbet igen. Make bør behandle `404 Job not found or expired` som et nyt
forsøg, ikke som et permanent dokumentproblem.

### Make får HTTP 401

Kontrollér, at værdien i `X-API-Key` er præcis den samme som
`SPLITTER_API_KEY` i Render, uden ekstra mellemrum.
