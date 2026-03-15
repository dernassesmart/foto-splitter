# Foto Splitter

Automatischer Scanner-Foto-Splitter: Legt ein Bild in den Eingangsordner, der Watcher erkennt es, schneidet die einzelnen Fotos heraus und speichert sie im Ausgangsordner.

## Funktionsweise

- Überwacht `/data/input` auf neue Bilder (JPG, PNG, TIFF, BMP)
- Erkennt Trennlinien zwischen Fotos via Helligkeits-Peak-Detektion
- Schneidet jedes Foto einzeln aus und speichert es in `/data/output`
- Verschiebt verarbeitete Scans nach `/data/input/done`
- Web-UI unter Port 8080 zeigt Live-Status

## Schnellstart (lokal testen)

```bash
pip install -r requirements.txt
INPUT_DIR=./input OUTPUT_DIR=./output python src/app.py
```

Dann http://localhost:8080 öffnen.

## Deployment auf Synology

### Option A: Image von GitHub Container Registry (empfohlen)

Nachdem du den Code in dein GitHub gepusht hast und der Action-Build durchgelaufen ist:

1. Synology Container Manager öffnen
2. "Projekt" → "Erstellen" → docker-compose.yml einfügen
3. Pfade anpassen:

```yaml
volumes:
  - /volume1/photos/scan-eingang:/data/input
  - /volume1/photos/scan-ausgang:/data/output
```

4. Image-Name in docker-compose.yml anpassen:
```yaml
image: ghcr.io/DEIN-GITHUB-USERNAME/foto-splitter:latest
```

### Option B: Lokal bauen auf Synology

```bash
docker build -t foto-splitter .
docker-compose up -d
```

## Einstellungen

Entweder via Umgebungsvariablen in docker-compose.yml oder live in der Web-UI:

| Variable     | Default | Beschreibung |
|-------------|---------|--------------|
| `PROMINENCE` | `46`    | Empfindlichkeit der Trennlinienerkennung |
| `PADDING`    | `0`     | Rand in Pixeln um jedes Foto |
| `ROTATION`   | `0`     | Rotation: 0, 90, -90 oder 180 Grad |

## GitHub Actions

Der Workflow `.github/workflows/docker.yml` baut automatisch bei jedem Push auf `main`:
- Multi-Arch Image (AMD64 + ARM64) für alle Synology-Modelle
- Wird in GitHub Container Registry (ghcr.io) gepusht
- Kein Docker Hub Account nötig

## Ordnerstruktur

```
/data/input/          ← Scans hier reinlegen
/data/input/done/     ← Verarbeitete Scans landen hier
/data/output/         ← Einzelne Fotos kommen hier raus
```

## Dateinamen

Aus `scan001.jpg` mit 4 Fotos werden:
- `scan001_foto1.jpg`
- `scan001_foto2.jpg`
- `scan001_foto3.jpg`
- `scan001_foto4.jpg`
