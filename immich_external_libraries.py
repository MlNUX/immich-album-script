#!/usr/bin/env python3
"""Synchronisiert External-Library-Unterordner mit gleichnamigen Alben (beide Richtungen).

Pro direktem Unterordner einer Library wird ein Album mit dem Ordnernamen
angelegt (nur erste Ebene; tiefer liegende Bilder zaehlen zum Album ihres
obersten Unterordners). Bilder direkt im Wurzelordner der Library werden
uebersprungen.

Ein Lauf fuehrt mehrere Phasen aus (Reihenfolge ist wichtig, damit auch geteilte
Ordner, manuell abgelegte Dateien und Loeschungen fuer ALLE Nutzer korrekt sind):

  0. Loescht ein Nutzer ein externes Album-Bild (-> Papierkorb), wird dessen
     DATEI im Ordner geloescht und das Asset bei ALLEN Nutzern endgueltig
     entfernt ('einmal loeschen = ueberall weg'). Unwiderruflich.
  1. Ergaenzte Uploads aus den Unterordner-Alben in den passenden Unterordner der
     jeweiligen Library kopieren (deterministischer Dateiname, keine Duplikate).
  2. ALLE Libraries scannen und warten, bis der Import durch ist (faengt auch
     manuell in den Ordner gelegte Dateien ab).
  3. Alle Library-Assets in die (ggf. neu angelegten) Unterordner-Alben
     aufnehmen - so sehen alle Nutzer denselben Ordnerinhalt in ihrem Album.
  4. Die erfolgreich importierten Uploads ENDGUELTIG loeschen (kein Papierkorb).

Owner-Modell / Pro-User Keys:
    Ein Album gehoert immer dem Nutzer, dem der benutzte API-Key gehoert, und
    die Immich-Suche liefert nur Assets dieses Nutzers. Ein Admin kann NICHT auf
    die Assets anderer Nutzer zugreifen. Damit auch fremde Libraries verarbeitet
    werden, wird pro Library der API-Key des jeweiligen Owners benutzt.

    - ADMIN_API_KEY: listet Nutzer + Libraries und wird automatisch fuer den
      eigenen Nutzer (dem der Admin-Key gehoert) verwendet.
    - USER_API_KEYS: Zuordnung weiterer Nutzer -> deren API-Key (per E-Mail
      oder User-ID). Nutzer ohne hinterlegten Key werden uebersprungen.

Beispiele:
    ./immich_external_libraries.py                 # beide Richtungen, alle Libraries
    ./immich_external_libraries.py --library test1 # nur Library 'test1'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from urllib import error, request

# ===========================================================================
# HIER EINTRAGEN: Domain + Admin-API-Key deiner Immich-Instanz.
IMMICH_URL = "DOMAIN"
ADMIN_API_KEY = "APIKEY"

# Weitere Nutzer -> deren eigener API-Key (Key im Web-UI unter
# Account-Einstellungen -> API Keys erstellen). Schluessel darf E-Mail oder
# User-ID sein. Der Admin-Nutzer selbst muss hier NICHT eingetragen werden.
USER_API_KEYS: dict[str, str] = {
    "EMAIL": "APIKEY",
}

# Fuer die Rueckrichtung noetig: Mapping vom Immich-Import-Pfad (wie in der Library
# hinterlegt, z.B. /external/test) zum tatsaechlichen Pfad auf DIESEM Host.
# Laeuft Immich in Docker, ist /external der Container-Pfad; auf dem Host liegt
# der Ordner per Volume-Mount evtl. woanders. Laengster passender Praefix
# gewinnt. Beispiel: {"/external": "/mnt/photos"}.
IMPORT_PATH_MAP: dict[str, str] = {
    "/external": "/mnt/external",
}
# ===========================================================================

CHUNK = 500        # Assets pro Album-Add-Request
SCAN_TIMEOUT = 180  # Sekunden, die auf den Import nach einem Library-Scan gewartet wird
SCAN_INTERVAL = 3   # Sekunden zwischen den Poll-Versuchen


def api_request(base_url: str, path: str, api_key: str, method: str = "GET",
                body: dict | None = None, tolerant: bool = False):
    """Fuehrt einen Request gegen die Immich API aus und liefert JSON zurueck.

    Mit tolerant=True fuehrt ein HTTP-Fehler nicht zum Abbruch, sondern gibt eine
    Warnung aus und liefert None zurueck (fuer nicht-kritische Loeschungen).
    """
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if tolerant:
            print(f"  WARNUNG: HTTP {exc.code} bei {method} {path}: {detail}")
            return None
        sys.exit(f"HTTP {exc.code} bei {method} {path}: {detail}")
    except error.URLError as exc:
        sys.exit(f"Verbindungsfehler bei {method} {path}: {exc.reason}")


def get_users(base_url: str, api_key: str) -> dict[str, dict]:
    """Liefert ein Mapping user_id -> user-Objekt (Admin-Endpoint)."""
    users = api_request(base_url, "/api/users", api_key)
    return {u["id"]: u for u in users}


def get_me(base_url: str, api_key: str) -> dict:
    """Liefert den Nutzer, dem der API-Key gehoert."""
    return api_request(base_url, "/api/users/me", api_key)


def get_external_libraries(base_url: str, api_key: str) -> list[dict]:
    """Liefert alle External Libraries.

    Neuere Immich-Versionen kennen nur noch External Libraries und haben das
    'type'-Feld entfernt. Aeltere Versionen liefern es mit, daher filtern wir
    defensiv auf EXTERNAL wenn das Feld vorhanden ist.
    """
    libs = api_request(base_url, "/api/libraries", api_key)
    return [lib for lib in libs if lib.get("type", "EXTERNAL") == "EXTERNAL"]


def search_all(base_url: str, api_key: str, filters: dict) -> list[dict]:
    """Liefert alle Asset-Objekte einer Metadaten-Suche (durchpaginiert)."""
    items: list[dict] = []
    page = 1
    while True:
        result = api_request(
            base_url, "/api/search/metadata", api_key, method="POST",
            body={**filters, "page": page, "size": 1000},
        )
        bucket = result.get("assets", {})
        items.extend(bucket.get("items", []))
        next_page = bucket.get("nextPage")
        if not next_page:
            break
        page = int(next_page)
    return items


def get_albums_by_name(base_url: str, api_key: str) -> dict[str, dict]:
    """Liefert alle Alben des Key-Besitzers als Mapping albumName -> Album."""
    return {a.get("albumName"): a
            for a in api_request(base_url, "/api/albums", api_key)}


def add_assets_to_album(base_url: str, api_key: str, album_id: str,
                        asset_ids: list[str]) -> tuple[int, int]:
    """Fuegt Assets in Bloecken hinzu. Liefert (hinzugefuegt, bereits_vorhanden)."""
    added = existed = 0
    for i in range(0, len(asset_ids), CHUNK):
        chunk = asset_ids[i:i + CHUNK]
        results = api_request(
            base_url, f"/api/albums/{album_id}/assets", api_key,
            method="PUT", body={"ids": chunk},
        ) or []
        for r in results:
            if r.get("success"):
                added += 1
            elif r.get("error") == "duplicate":
                existed += 1
    return added, existed


def create_album(base_url: str, api_key: str, name: str,
                 asset_ids: list[str]) -> dict:
    """Legt ein neues Album an (optional direkt mit Assets)."""
    return api_request(
        base_url, "/api/albums", api_key, method="POST",
        body={"albumName": name, "assetIds": asset_ids},
    )


def download_original(base_url: str, api_key: str, asset_id: str) -> bytes:
    """Laedt die Originaldatei eines Assets als Bytes."""
    url = base_url.rstrip("/") + f"/api/assets/{asset_id}/original"
    req = request.Request(url, headers={"x-api-key": api_key})
    try:
        with request.urlopen(req, timeout=300) as resp:
            return resp.read()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        sys.exit(f"HTTP {exc.code} beim Download von Asset {asset_id}: {detail}")
    except error.URLError as exc:
        sys.exit(f"Verbindungsfehler beim Download von Asset {asset_id}: {exc.reason}")


def delete_assets(base_url: str, api_key: str, asset_ids: list[str],
                  force: bool = False, tolerant: bool = False) -> None:
    """Loescht Assets (default: in den Papierkorb; force=True: endgueltig)."""
    api_request(base_url, "/api/assets", api_key, method="DELETE",
                body={"ids": asset_ids, "force": force}, tolerant=tolerant)


def scan_library(base_url: str, admin_key: str, library_id: str) -> None:
    """Stoesst einen Scan der Library an (importiert neue Dateien)."""
    api_request(base_url, f"/api/libraries/{library_id}/scan", admin_key,
                method="POST")


def map_to_host_path(import_path: str) -> str | None:
    """Wandelt einen Immich-Import-Pfad in den Host-Pfad um (laengster Praefix)."""
    for prefix in sorted(IMPORT_PATH_MAP, key=len, reverse=True):
        if import_path == prefix or import_path.startswith(prefix.rstrip("/") + "/"):
            host_prefix = IMPORT_PATH_MAP[prefix]
            rest = import_path[len(prefix.rstrip("/")):]
            return host_prefix.rstrip("/") + rest
    return None


def asset_subfolder(original_path: str, import_paths: list[str]) -> str | None:
    """Liefert den direkten Unterordner eines Assets relativ zum Import-Pfad.

    Beispiel: importPath '/external/lib', originalPath
    '/external/lib/urlaub/2024/img.jpg' -> 'urlaub' (nur erste Ebene, tiefer
    liegende Ordner zaehlen zu diesem Album mit). Liegt die Datei direkt im
    Wurzelordner der Library (kein Unterordner), wird None geliefert.
    """
    for base in sorted(import_paths, key=len, reverse=True):
        prefix = base.rstrip("/") + "/"
        if original_path.startswith(prefix):
            parts = original_path[len(prefix):].split("/")
            # Mindestens Unterordner + Dateiname, sonst liegt die Datei in der Wurzel.
            if len(parts) >= 2 and parts[0]:
                return parts[0]
            return None
    return None


def group_assets_by_subfolder(assets: list[dict],
                              import_paths: list[str]) -> dict[str, list[str]]:
    """Gruppiert Assets nach ihrem direkten Unterordner -> Liste von Asset-IDs.

    Assets ohne Unterordner (direkt im Library-Wurzelordner) werden ausgelassen.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for a in assets:
        folder = asset_subfolder(a.get("originalPath") or "", import_paths)
        if folder:
            groups[folder].append(a["id"])
    return groups


def get_library_file_map(base_url: str, api_key: str,
                         library_id: str) -> dict[str, str]:
    """Liefert ein Mapping originalFileName -> asset_id fuer eine Library."""
    return {a.get("originalFileName"): a["id"]
            for a in search_all(base_url, api_key, {"libraryId": library_id})}


def resolve_owner_keys(base_url: str, admin_key: str,
                       users: dict[str, dict]) -> dict[str, str]:
    """Baut ein Mapping owner_id -> API-Key.

    Enthaelt den Admin-Key fuer dessen eigenen Nutzer sowie alle in
    USER_API_KEYS hinterlegten Nutzer (per E-Mail oder ID aufgeloest).
    """
    email_to_id = {u.get("email"): uid for uid, u in users.items()}
    resolved: dict[str, str] = {}

    # Admin-Key automatisch fuer den eigenen Nutzer registrieren.
    me = get_me(base_url, admin_key)
    resolved[me["id"]] = admin_key

    for ident, key in USER_API_KEYS.items():
        if not key:
            continue
        uid = ident if ident in users else email_to_id.get(ident)
        if uid is None:
            print(f"Warnung: Nutzer '{ident}' aus USER_API_KEYS nicht gefunden.")
            continue
        resolved[uid] = key
    return resolved


def library_owner_label(lib: dict, users: dict[str, dict]) -> str:
    owner = users.get(lib.get("ownerId"), {})
    return owner.get("email") or owner.get("name") or lib.get("ownerId")


def libraries_with_key(users, owner_keys, libraries):
    """Iteriert Libraries als (lib, label, key); ohne API-Key -> Meldung + Skip."""
    for lib in libraries:
        name = lib.get("name") or "(ohne Name)"
        label = f"Library '{name}' (Owner: {library_owner_label(lib, users)})"
        key = owner_keys.get(lib.get("ownerId"))
        if key is None:
            print(f"\n{label}: uebersprungen - kein API-Key fuer diesen Nutzer "
                  f"hinterlegt (USER_API_KEYS).")
            continue
        yield lib, label, key


def propagate_deletions(args, users, owner_keys, libraries) -> None:
    """Phase 0: 'ueberall loeschen'.

    Loescht ein Nutzer ein externes Album-Bild, landet dessen Asset im
    Papierkorb (isTrashed). Diese Funktion sammelt solche getrashten externen
    Assets, loescht die zugehoerige DATEI im Ordner und entfernt das Asset bei
    ALLEN Nutzern endgueltig - so verschwindet das Bild ueberall.
    """
    # 1. Externe Assets aller Nutzer laden (einmal, wird in Schritt 3
    #    wiederverwendet); getrashte -> deren Datei-Pfade sammeln.
    deleted_paths: set[str] = set()
    per_lib: list[tuple[str, str, list[dict]]] = []
    for lib in libraries:
        key = owner_keys.get(lib.get("ownerId"))
        if key is None:
            continue
        owner_label = library_owner_label(lib, users)
        items = search_all(args.url, key,
                           {"libraryId": lib["id"], "withDeleted": True})
        per_lib.append((owner_label, key, items))
        trashed = [a for a in items
                   if a.get("isTrashed") and a.get("originalPath")]
        deleted_paths.update(a["originalPath"] for a in trashed)
        if trashed:
            print(f"  {owner_label}: {len(trashed)} geloeschte externe Assets erkannt")

    if not deleted_paths:
        print("  keine geloeschten Album-Bilder gefunden.")
        return

    # 2. Die zugehoerigen Dateien im Ordner loeschen.
    for cpath in sorted(deleted_paths):
        host = map_to_host_path(cpath)
        if host is None:
            print(f"  WARNUNG: Pfad {cpath} nicht in IMPORT_PATH_MAP - uebersprungen")
            continue
        try:
            os.remove(host)
            print(f"  Datei geloescht: {host}")
        except FileNotFoundError:
            print(f"  Datei bereits weg: {host}")

    # 3. Assets mit diesen Pfaden bei ALLEN Nutzern endgueltig entfernen
    #    (auch die noch vorhandene Kopie anderer Accounts).
    for owner_label, key, items in per_lib:
        ids = [a["id"] for a in items if a.get("originalPath") in deleted_paths]
        if ids:
            delete_assets(args.url, key, ids, force=True, tolerant=True)
            print(f"  {owner_label}: {len(ids)} Assets endgueltig entfernt")


def run_forward(args, users, owner_keys, libraries) -> None:
    """Library -> Alben: legt pro direktem Unterordner ein Album (Ordnername) an.

    Bilder direkt im Wurzelordner der Library (ohne Unterordner) werden
    uebersprungen; nur Unterordner werden zu Alben.
    """
    for lib, label, key in libraries_with_key(users, owner_keys, libraries):
        import_paths = lib.get("importPaths") or []
        assets = search_all(args.url, key, {"libraryId": lib["id"]})
        groups = group_assets_by_subfolder(assets, import_paths)
        grouped = sum(len(v) for v in groups.values())
        print(f"\n{label}: {len(assets)} Assets, "
              f"{len(groups)} Unterordner ({grouped} Assets)")

        skipped = len(assets) - grouped
        if skipped:
            print(f"  {skipped} Assets ohne Unterordner uebersprungen")
        if not groups:
            print("  -> keine Unterordner-Assets (evtl. noch nicht gescannt?)")
            continue

        albums = get_albums_by_name(args.url, key)
        for folder, asset_ids in sorted(groups.items()):
            existing = albums.get(folder)
            if existing:
                added, existed = add_assets_to_album(
                    args.url, key, existing["id"], asset_ids)
                print(f"  -> Album '{folder}' vorhanden ({existing['id']}): "
                      f"{added} neu hinzugefuegt, {existed} schon drin")
            else:
                album = create_album(args.url, key, folder, asset_ids)
                print(f"  -> Album '{folder}' angelegt ({album['id']}) "
                      f"mit {len(asset_ids)} Assets")


def collect_uploads(args, users, owner_keys, libraries) -> list[dict]:
    """Phase 1: Kopiert im Album ergaenzte Uploads in den Library-Ordner.

    Liefert eine Liste offener Loeschungen (je ein Dict mit key, upload_id,
    filename, library_id), die nach erfolgreichem Import verarbeitet werden.
    Der Dateiname ist deterministisch ('<upload_id>_<name>'), damit ein erneuter
    Lauf dieselbe Datei ueberschreibt statt Duplikate anzulegen.
    """
    pending: list[dict] = []
    for lib, label, key in libraries_with_key(users, owner_keys, libraries):
        print(f"\n{label}:")

        import_paths = lib.get("importPaths") or []
        host_root = map_to_host_path(import_paths[0]) if import_paths else None
        if host_root is None:
            print(f"  -> uebersprungen - Import-Pfad {import_paths} nicht in "
                  f"IMPORT_PATH_MAP gemappt.")
            continue

        lib_assets = search_all(args.url, key, {"libraryId": lib["id"]})
        library_ids = {a["id"] for a in lib_assets}
        groups = group_assets_by_subfolder(lib_assets, import_paths)
        if not groups:
            print("  -> keine Unterordner-Alben vorhanden.")
            continue

        albums = get_albums_by_name(args.url, key)
        for folder in sorted(groups):
            album = albums.get(folder)
            if album is None:
                print(f"  Ordner '{folder}': kein Album (wird in Phase 3 angelegt).")
                continue

            album_assets = search_all(args.url, key, {"albumIds": [album["id"]]})
            candidates = [a for a in album_assets if a["id"] not in library_ids]
            print(f"  Ordner '{folder}': Album {len(album_assets)} Assets, "
                  f"ergaenzte Uploads: {len(candidates)}")
            if not candidates:
                continue

            host_dir = os.path.join(host_root, folder)
            if not os.path.isdir(host_dir):
                print(f"  -> FEHLER: Ordner '{host_dir}' existiert nicht/kein "
                      f"Zugriff. Bitte IMPORT_PATH_MAP pruefen.")
                continue

            for a in candidates:
                fname = f"{a['id']}_{a.get('originalFileName')}"
                dest = os.path.join(host_dir, fname)
                with open(dest, "wb") as fh:
                    fh.write(download_original(args.url, key, a["id"]))
                pending.append({"key": key, "upload_id": a["id"],
                                "filename": fname, "library_id": lib["id"]})
                print(f"    kopiert -> {host_dir}/{fname}")
    return pending


def scan_and_wait(args, owner_keys, libraries, pending) -> dict:
    """Phase 2: Stoesst fuer ALLE Libraries einen Scan an und wartet auf Import.

    Wartet, bis alle kopierten Dateien importiert sind UND die Asset-Zahlen
    stabil bleiben (faengt auch manuell in den Ordner gelegte Dateien ab).
    Liefert ein Mapping (library_id, filename) -> asset_id der importierten.
    """
    for lib in libraries:
        scan_library(args.url, args.admin_key, lib["id"])
    print(f"Scans fuer {len(libraries)} Libraries angestossen, warte auf Import ...")

    want: dict[str, set] = defaultdict(set)
    for p in pending:
        want[p["library_id"]].add(p["filename"])

    imported: dict = {}
    prev_counts: dict = {}
    stable_rounds = 0
    deadline = time.time() + SCAN_TIMEOUT
    while time.time() < deadline:
        counts: dict = {}
        all_done = True
        for lib in libraries:
            key = owner_keys.get(lib.get("ownerId"))
            if key is None:
                continue
            file_map = get_library_file_map(args.url, key, lib["id"])
            counts[lib["id"]] = len(file_map)
            for fname in want[lib["id"]]:
                if fname in file_map:
                    imported[(lib["id"], fname)] = file_map[fname]
                else:
                    all_done = False
        stable_rounds = stable_rounds + 1 if counts == prev_counts else 0
        prev_counts = counts
        if all_done and stable_rounds >= 1:
            break
        time.sleep(SCAN_INTERVAL)
    return imported


def delete_imported_uploads(args, pending, imported) -> None:
    """Phase 4: Loescht nur die Uploads endgueltig, deren Datei importiert wurde."""
    to_delete: dict[str, list] = defaultdict(list)
    undeleted: list[str] = []
    for p in pending:
        if (p["library_id"], p["filename"]) in imported:
            to_delete[p["key"]].append(p["upload_id"])
        else:
            undeleted.append(p["filename"])

    total = sum(len(v) for v in to_delete.values())
    for key, ids in to_delete.items():
        delete_assets(args.url, key, ids, force=True)
    print(f"{total} importierte Uploads endgueltig geloescht.")
    if undeleted:
        print(f"WARNUNG: {len(undeleted)} Datei(en) nicht als importiert erkannt, "
              f"deren Uploads bleiben erhalten: {', '.join(sorted(undeleted))}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="External-Library-Unterordner <-> gleichnamige Alben synchronisieren.")
    parser.add_argument("--url", default=IMMICH_URL,
                        help="Basis-URL der Immich-Instanz (Standard: oben im Script)")
    parser.add_argument("--admin-key", default=ADMIN_API_KEY,
                        help="Admin-API-Key (Standard: oben im Script)")
    parser.add_argument("--library",
                        help="Nur diese Library (exakter Name) verarbeiten.")
    args = parser.parse_args()

    if not args.url or args.admin_key in ("", "DEIN_API_KEY_HIER"):
        parser.error("Bitte IMMICH_URL und ADMIN_API_KEY oben im Script eintragen "
                     "(oder --url / --admin-key angeben).")

    users = get_users(args.url, args.admin_key)
    owner_keys = resolve_owner_keys(args.url, args.admin_key, users)
    libraries = get_external_libraries(args.url, args.admin_key)

    if args.library:
        libraries = [l for l in libraries if l.get("name") == args.library]
        if not libraries:
            sys.exit(f"Keine External Library mit Namen '{args.library}' gefunden.")

    if not libraries:
        sys.exit("Keine External Libraries gefunden.")

    # Ablauf in Phasen, damit auch geteilte Ordner, manuell abgelegte Dateien
    # und Loeschungen fuer ALLE Nutzer korrekt wirken:
    #   0. geloeschte (getrashte) Album-Bilder ueberall entfernen (Datei + Assets)
    #   1. ergaenzte Uploads aus den Alben in die passenden Unterordner kopieren
    #   2. ALLE Libraries scannen und auf den Import warten
    #   3. alle Library-Assets in die (ggf. neu angelegten) Unterordner-Alben aufnehmen
    #   4. die erfolgreich importierten Uploads endgueltig loeschen
    print("=== Phase 0: geloeschte Album-Bilder ueberall entfernen ===")
    propagate_deletions(args, users, owner_keys, libraries)

    print("\n=== Phase 1: ergaenzte Uploads -> Library-Ordner ===")
    pending = collect_uploads(args, users, owner_keys, libraries)

    print("\n=== Phase 2: Libraries scannen & auf Import warten ===")
    imported = scan_and_wait(args, owner_keys, libraries, pending)

    print("\n=== Phase 3: Library-Assets -> Alben ===")
    run_forward(args, users, owner_keys, libraries)

    print("\n=== Phase 4: importierte Uploads loeschen ===")
    delete_imported_uploads(args, pending, imported)


if __name__ == "__main__":
    main()
