#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from urllib import error, request

# ===========================================================================
# Zugangsdaten (URL + API-Keys) und Pfad-Mappings stehen in einer YAML-Config,
# standardmaessig 'config.yaml' neben diesem Script (per --config aenderbar).
# Vorlage: 'config.example.yaml'. Die folgenden Globals werden beim Start aus
# der Config befuellt (siehe load_config()).
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "config.yaml")

IMMICH_URL = ""
ADMIN_API_KEY = ""
USER_API_KEYS: dict[str, str] = {}
IMPORT_PATH_MAP: dict[str, str] = {}
# Wurzel (Immich-Container-Pfad), unter der die Permutations-Ordner der
# Share-Funktion liegen, z.B. "/external". Muss ueber IMPORT_PATH_MAP auf einen
# Host-Pfad abbildbar sein. Leer => Share-Sync (Phase S) ist deaktiviert.
EXTERNAL_ROOT = ""
# ===========================================================================

CHUNK = 500        # Assets pro Album-Add-Request
SCAN_TIMEOUT = 180  # Sekunden, die auf den Import nach einem Library-Scan gewartet wird
SCAN_INTERVAL = 3   # Sekunden zwischen den Poll-Versuchen
MANIFEST_NAME = ".immich_groups.json"  # Zustandsdatei im External-Wurzelordner


def _unquote(s: str) -> str:
    """Entfernt umschliessende einfache/doppelte Anfuehrungszeichen."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _parse_simple_yaml(text: str) -> dict:
    """Minimaler YAML-Parser fuer das Config-Format (Fallback ohne PyYAML).

    Unterstuetzt genau, was die Config braucht: Top-Level 'key: value'-Skalare
    sowie eine Ebene eingerueckter 'key: value'-Maps. Kommentare (#), Leerzeilen
    und Anfuehrungszeichen werden beruecksichtigt.
    """
    result: dict = {}
    current_map: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        # Ganzzeilige Kommentare und Leerzeilen ueberspringen.
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # Inline-Kommentar (nur nach Whitespace) abschneiden.
        if " #" in line:
            line = line[:line.index(" #")].rstrip()
        if ":" not in line:
            continue
        indented = line[0] in " \t"
        k, _, v = line.strip().partition(":")
        key, val = _unquote(k.strip()), _unquote(v.strip())
        if indented:
            if current_map is not None:
                current_map[key] = val
        elif val == "":            # leerer Wert -> beginnt eine eingerueckte Map
            current_map = {}
            result[key] = current_map
        else:
            result[key] = val
            current_map = None
    return result


def load_config(path: str) -> None:
    """Laedt URL, API-Keys und Pfad-Mappings aus der YAML-Config in die Globals.

    Nutzt PyYAML, falls installiert, sonst den eingebauten Minimal-Parser.
    """
    global IMMICH_URL, ADMIN_API_KEY, USER_API_KEYS, IMPORT_PATH_MAP
    if not os.path.exists(path):
        sys.exit(f"Config-Datei '{path}' nicht gefunden. Kopiere config.example.yaml "
                 f"nach config.yaml und trage deine Daten ein.")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        import yaml  # optional, robuster als der Fallback
        data = yaml.safe_load(text) or {}
    except ModuleNotFoundError:
        data = _parse_simple_yaml(text)

    global EXTERNAL_ROOT
    IMMICH_URL = data.get("immich_url") or ""
    ADMIN_API_KEY = data.get("admin_api_key") or ""
    USER_API_KEYS = data.get("user_api_keys") or {}
    IMPORT_PATH_MAP = data.get("import_path_map") or {}
    EXTERNAL_ROOT = data.get("external_root") or ""


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


def create_library(base_url: str, admin_key: str, owner_id: str, name: str,
                   import_path: str) -> dict:
    """Legt eine External Library fuer einen Nutzer auf genau einen Import-Pfad an.

    Library-Verwaltung ist Admin-Sache, daher wird der Admin-Key benutzt; der
    'ownerId' bestimmt, wem die Library (und damit die importierten Assets)
    gehoert.
    """
    return api_request(
        base_url, "/api/libraries", admin_key, method="POST",
        body={"ownerId": owner_id, "name": name, "importPaths": [import_path]},
    )


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


# ---------------------------------------------------------------------------
# Phase S: geteilte Alben -> External-Permutationsordner (append-only).
#
# Idee: Teilt ein Nutzer sein Album mit anderen, sollen ALLE Beteiligten die
# Bilder gleichberechtigt als eigene External-Assets bekommen - statt des
# Immich-"Owner + Gast"-Modells. Dazu werden die Bilder in einen Ordner
# external/<hash der Nutzergruppe>/<albumname>/ verschoben und jedes
# Gruppenmitglied bekommt eine eigene External Library auf external/<hash>.
# Die restliche Arbeit (pro Nutzer ein gleichnamiges Album) macht die
# bestehende Ordner<->Album-Engine (Phasen 2-4).
#
# Mitgliedschaft ist bewusst append-only: es kann nur jemand DAZUkommen. Waechst
# die Gruppe (z.B. {A,B} -> {A,B,C}), zieht der Album-Ordner in den groesseren
# Permutationsordner um. Permutationsordner + Libraries werden nie geloescht
# (auch leer nicht) und bei gleicher Gruppe wiederverwendet.
# ---------------------------------------------------------------------------


def group_hash(member_ids: list[str]) -> str:
    """Deterministischer Kurz-Hash einer Nutzergruppe (Reihenfolge egal)."""
    joined = "\n".join(sorted(member_ids))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _album_user_id(u: dict) -> str | None:
    return (u.get("user") or {}).get("id") or u.get("userId")


def album_owner_id(album: dict) -> str | None:
    """Owner-ID eines Albums.

    Immich v3 hat 'ownerId'/'owner' aus dem Album entfernt - der Owner ist nun
    der albumUsers-Eintrag mit role 'owner'. Fuer aeltere Versionen faellt die
    Funktion auf 'ownerId'/'owner.id' zurueck.
    """
    for u in album.get("albumUsers") or []:
        if u.get("role") == "owner":
            return _album_user_id(u)
    return album.get("ownerId") or (album.get("owner") or {}).get("id")


def album_members(album: dict) -> list[str]:
    """Liefert die sortierte Mitglieder-Liste eines Albums (Owner + geteilt-mit)."""
    ids = {album_owner_id(album)}
    for u in album.get("albumUsers") or []:
        uid = _album_user_id(u)
        if uid:
            ids.add(uid)
    return sorted(i for i in ids if i)


def load_manifest(host_root: str) -> dict:
    """Laedt die Zustandsdatei (welches Album gehoert zu welcher Gruppe)."""
    path = os.path.join(host_root, MANIFEST_NAME)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("albums", {})
        data.setdefault("groups", {})
        return data
    return {"albums": {}, "groups": {}}


def save_manifest(host_root: str, manifest: dict) -> None:
    path = os.path.join(host_root, MANIFEST_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)


def user_label(users: dict, uid: str) -> str:
    return users.get(uid, {}).get("email") or uid


def collect_shared_albums(args, users, owner_keys) -> list[dict]:
    """Sammelt alle nativ geteilten QUELL-Alben, deren Owner einen API-Key hat.

    Nur eigene Alben des jeweiligen Key-Besitzers (nicht die Gast-Kopien anderer)
    und nur solche mit mindestens einem weiteren Mitglied. Fuer geteilte Alben
    ohne albumUsers in der Listenansicht wird das Album-Detail nachgeladen.
    """
    seen: set[str] = set()
    result: list[dict] = []
    for owner_id, key in owner_keys.items():
        albums = api_request(args.url, "/api/albums", key) or []
        owned = 0
        shared_here = 0
        foreign: list[dict] = []
        for album in albums:
            if album["id"] in seen:
                continue
            # Der Owner steckt in albumUsers - liefert die Listen-Ansicht das
            # nicht mit, muss das Album-Detail geladen werden.
            if not album.get("albumUsers"):
                detail = api_request(args.url, f"/api/albums/{album['id']}", key)
                if detail:
                    album = detail
            if album_owner_id(album) != owner_id:
                foreign.append(album)
                continue
            owned += 1
            members = album_members(album)
            if len(members) < 2:
                continue  # nicht geteilt -> nicht Teil einer Gruppe
            shared_here += 1
            seen.add(album["id"])
            result.append({"id": album["id"], "name": album.get("albumName"),
                           "owner_id": owner_id, "key": key, "members": members})
        print(f"  {user_label(users, owner_id)}: {len(albums)} sichtbar, "
              f"{owned} eigene, {shared_here} davon geteilt")
        for a in foreign:
            print(f"      (fremd) '{a.get('albumName')}' gehoert "
                  f"{user_label(users, album_owner_id(a))} - Key fehlt")
    return result


def libs_owning_path(libraries: list[dict], container_path: str) -> set[str]:
    """Owner-IDs, die bereits eine Library auf container_path haben."""
    target = container_path.rstrip("/")
    return {lib["ownerId"] for lib in libraries
            if target in [ip.rstrip("/") for ip in (lib.get("importPaths") or [])]}


def ensure_group_libraries(args, users, members, ghash, libraries) -> list[dict]:
    """Stellt sicher, dass jedes Gruppenmitglied eine Library auf external/<hash> hat.

    Legt fehlende an (idempotent, da Ordner/Libraries wiederverwendet werden) und
    liefert die ergaenzte Library-Liste zurueck.
    """
    container = f"{EXTERNAL_ROOT.rstrip('/')}/{ghash}"
    have = libs_owning_path(libraries, container)
    for uid in members:
        if uid in have:
            continue
        if uid not in users:
            print(f"      WARNUNG: Nutzer {uid} unbekannt - keine Library angelegt.")
            continue
        lib = create_library(args.url, args.admin_key, uid, f"shared-{ghash}",
                             container)
        libraries.append(lib)
        print(f"      Library angelegt fuer {user_label(users, uid)} -> {container}")
    return libraries


def sync_shared_albums(args, users, owner_keys) -> list[dict]:
    """Phase S: geteilte Alben externalisieren bzw. in groessere Gruppe umziehen.

    Liefert offene Original-Loeschungen im selben Format wie collect_uploads
    ({key, upload_id, filename, library_id}), damit scan_and_wait /
    delete_imported_uploads sie nach dem Import mitverarbeiten.
    """
    host_root = map_to_host_path(EXTERNAL_ROOT)
    if host_root is None:
        print(f"  uebersprungen: external_root '{EXTERNAL_ROOT}' nicht in "
              f"import_path_map gemappt.")
        return []

    manifest = load_manifest(host_root)
    albums = collect_shared_albums(args, users, owner_keys)
    if not albums:
        print("  keine geteilten Alben gefunden.")
        return []

    libraries = get_external_libraries(args.url, args.admin_key)
    pending: list[dict] = []

    for alb in albums:
        members = alb["members"]
        new_hash = group_hash(members)
        prev = manifest["albums"].get(alb["id"])
        prev_hash = prev.get("hash") if prev else None

        labels = ", ".join(user_label(users, m) for m in members)
        print(f"\n  Album '{alb['name']}' (Owner {user_label(users, alb['owner_id'])}), "
              f"Gruppe [{labels}] -> {new_hash}")

        target_host_dir = os.path.join(host_root, new_hash, alb["name"])

        if prev_hash == new_hash:
            print("    Gruppe unveraendert - stelle nur Libraries sicher.")
            libraries = ensure_group_libraries(args, users, members, new_hash,
                                               libraries)
            continue

        os.makedirs(target_host_dir, exist_ok=True)

        if prev_hash is None:
            # Erst-Externalisierung: interne Album-Assets in den Ordner kopieren.
            assets = search_all(args.url, alb["key"], {"albumIds": [alb["id"]]})
            internal = [a for a in assets if not a.get("libraryId")]
            print(f"    Erst-Externalisierung: {len(internal)}/{len(assets)} "
                  f"interne Assets -> {new_hash}/{alb['name']}/")
            for a in internal:
                fname = f"{a['id']}_{a.get('originalFileName')}"
                with open(os.path.join(target_host_dir, fname), "wb") as fh:
                    fh.write(download_original(args.url, alb["key"], a["id"]))
                pending.append({"key": alb["key"], "upload_id": a["id"],
                                "filename": fname, "library_id": None,
                                "owner_id": alb["owner_id"], "group_hash": new_hash})
        else:
            # Gruppe gewachsen: Album-Ordner in den groesseren Permutationsordner
            # verschieben (alten leeren Ordner bewusst stehen lassen).
            old_host_dir = os.path.join(host_root, prev_hash, alb["name"])
            print(f"    Umzug {prev_hash} -> {new_hash}")
            if os.path.isdir(old_host_dir):
                for entry in os.listdir(old_host_dir):
                    shutil.move(os.path.join(old_host_dir, entry),
                                os.path.join(target_host_dir, entry))
            else:
                print(f"      WARNUNG: alter Ordner {old_host_dir} fehlt - "
                      f"nichts zu verschieben.")

        libraries = ensure_group_libraries(args, users, members, new_hash,
                                           libraries)
        manifest["albums"][alb["id"]] = {
            "name": alb["name"], "owner": alb["owner_id"],
            "members": members, "hash": new_hash}
        manifest["groups"][new_hash] = {"members": members}

    save_manifest(host_root, manifest)

    # Fuer die zu loeschenden Originale die Owner-Library der Gruppe nachtragen,
    # damit scan_and_wait den erfolgreichen Import bestaetigen kann.
    owner_lib = {(lib["ownerId"], ip.rstrip("/")): lib["id"]
                 for lib in libraries for ip in (lib.get("importPaths") or [])}
    for p in pending:
        container = f"{EXTERNAL_ROOT.rstrip('/')}/{p['group_hash']}"
        p["library_id"] = owner_lib.get((p["owner_id"], container.rstrip("/")))
    dropped = [p for p in pending if not p["library_id"]]
    if dropped:
        print(f"  WARNUNG: {len(dropped)} Originale ohne Owner-Library - werden "
              f"nicht geloescht (Import nicht bestaetigbar).")
    return [p for p in pending if p["library_id"]]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="External-Library-Unterordner <-> gleichnamige Alben synchronisieren.")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="Pfad zur YAML-Config (Standard: config.yaml neben dem Script)")
    parser.add_argument("--url",
                        help="Basis-URL der Immich-Instanz (ueberschreibt die Config)")
    parser.add_argument("--admin-key",
                        help="Admin-API-Key (ueberschreibt die Config)")
    parser.add_argument("--library",
                        help="Nur diese Library (exakter Name) verarbeiten.")
    args = parser.parse_args()

    load_config(args.config)
    args.url = args.url or IMMICH_URL
    args.admin_key = args.admin_key or ADMIN_API_KEY

    if not args.url or args.url == "DOMAIN":
        parser.error(f"Keine Immich-URL gesetzt. Trage 'immich_url' in {args.config} "
                     f"ein (oder nutze --url).")
    if not args.admin_key or args.admin_key == "APIKEY":
        parser.error(f"Kein Admin-API-Key gesetzt. Trage 'admin_api_key' in {args.config} "
                     f"ein (oder nutze --admin-key).")

    users = get_users(args.url, args.admin_key)
    owner_keys = resolve_owner_keys(args.url, args.admin_key, users)

    # Ablauf in Phasen (Details siehe README.md), damit auch geteilte Ordner,
    # manuell abgelegte Dateien und Loeschungen fuer ALLE Nutzer korrekt wirken.
    #
    # Phase S laeuft zuerst: sie legt Permutationsordner + Libraries an und
    # verschiebt geteilte Album-Bilder dorthin (kann also auf einer frischen
    # Instanz die ersten Libraries ueberhaupt erzeugen). Bei einem gezielten
    # --library-Lauf wird Phase S uebersprungen.
    share_pending: list[dict] = []
    if EXTERNAL_ROOT and not args.library:
        print("=== Phase S: geteilte Alben -> External ===")
        share_pending = sync_shared_albums(args, users, owner_keys)

    libraries = get_external_libraries(args.url, args.admin_key)
    if args.library:
        libraries = [l for l in libraries if l.get("name") == args.library]
        if not libraries:
            sys.exit(f"Keine External Library mit Namen '{args.library}' gefunden.")

    if not libraries:
        sys.exit("Keine External Libraries gefunden.")

    print("\n=== Phase 0: geloeschte Album-Bilder ueberall entfernen ===")
    propagate_deletions(args, users, owner_keys, libraries)

    print("\n=== Phase 1: ergaenzte Uploads -> Library-Ordner ===")
    pending = share_pending + collect_uploads(args, users, owner_keys, libraries)

    print("\n=== Phase 2: Libraries scannen & auf Import warten ===")
    imported = scan_and_wait(args, owner_keys, libraries, pending)

    print("\n=== Phase 3: Library-Assets -> Alben ===")
    run_forward(args, users, owner_keys, libraries)

    print("\n=== Phase 4: importierte Uploads loeschen ===")
    delete_imported_uploads(args, pending, imported)


if __name__ == "__main__":
    main()
