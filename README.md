# Mauritius Property Scout (standalone)

Runs every hour on GitHub for free, no Claude needed.

- `scout.py` – the scanner (reads property sites, finds matches, sends alerts)
- `config.yaml` – which websites to read
- `.github/workflows/scout.yml` – the hourly schedule
- `docs/` – the app (served by GitHub Pages); `docs/data/db.json` holds your matches and comparables
- `scanner-settings.json` – optional; download it from the app's **My searches** tab and upload it here to change what the scanner looks for

See SETUP-GUIDE.html for step-by-step setup.
