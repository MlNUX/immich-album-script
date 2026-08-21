# immich-album-script

Synchronisiert die direkten Unterordner einer Immich **External Library** mit
gleichnamigen **Alben** – in beide Richtungen und fuer alle Nutzer.

Pro direktem Unterordner einer Library wird ein Album mit dem Ordnernamen
angelegt (nur erste Ebene; tiefer liegende Bilder zaehlen zum Album ihres
obersten Unterordners). Bilder direkt im Wurzelordner der Library werden
uebersprungen.

## Setup

Voraussetzung: Python 3.9+. Es werden nur Module der Standardbibliothek
benoetigt. PyYAML wird genutzt, falls installiert – ist aber **nicht** noetig
(es gibt einen eingebauten Fallback-Parser fuer die Config).

Zugangsdaten in eine YAML-Config eintragen:

```sh
cp config.example.yaml config.yaml
# config.yaml mit deinen echten Werten ausfuellen
```

`config.yaml` enthaelt Secrets und ist per `.gitignore` vom Repo ausgeschlossen.

### Config-Felder (`config.yaml`)

```yaml
# Basis-URL + Admin-API-Key deiner Immich-Instanz.
immich_url: "https://immich.example.com"
admin_api_key: "APIKEY"

# Weitere Nutzer -> deren eigener API-Key (E-Mail oder User-ID als Schluessel).
# Der Admin-Nutzer selbst muss hier NICHT eingetragen werden.
user_api_keys:
  "user@example.com": "APIKEY"

# Immich-Import-Pfad -> tatsaechlicher Host-Pfad (laengster Praefix gewinnt).
import_path_map:
  "/external": "/mnt/external"
```

- **`immich_url`** – Basis-URL der Immich-Instanz.
- **`admin_api_key`** – Admin-API-Key. Listet Nutzer + Libraries und wird
  automatisch fuer den eigenen Nutzer (dem der Key gehoert) verwendet.
- **`user_api_keys`** – Zuordnung weiterer Nutzer zu deren API-Key (per E-Mail
  oder User-ID). Nutzer ohne hinterlegten Key werden uebersprungen. Noetig, weil
  ein Album immer dem Key-Besitzer gehoert und die Immich-Suche nur dessen
  Assets liefert – ein Admin kann **nicht** auf fremde Assets zugreifen. Damit
  auch fremde Libraries verarbeitet werden, wird pro Library der Key des
  jeweiligen Owners benutzt.
- **`import_path_map`** – Mapping vom Immich-Import-Pfad (wie in der Library
  hinterlegt, z.B. `/external/test`) zum tatsaechlichen Pfad auf **diesem** Host.
  Laeuft Immich in Docker, ist `/external` der Container-Pfad; auf dem Host liegt
  der Ordner per Volume-Mount evtl. woanders. Laengster passender Praefix
  gewinnt.
- **`external_root`** *(optional)* – Wurzel-Container-Pfad fuer den **Share-Sync**
  (siehe unten). Muss ueber `import_path_map` auf einen Host-Pfad abbildbar sein.
  Leer/weggelassen ⇒ Share-Sync ist aus.

Den API-Key erstellt man im Web-UI unter **Account-Einstellungen → API Keys**.

## Nutzung

```sh
./immich_external_libraries.py                 # beide Richtungen, alle Libraries
./immich_external_libraries.py --library test1 # nur Library 'test1'
./immich_external_libraries.py --config /pfad/zu/config.yaml
./immich_external_libraries.py --url ... --admin-key ...  # ueberschreibt die Config
```

`--url` und `--admin-key` ueberschreiben die Werte aus der Config; `--config`
waehlt eine andere Config-Datei (Standard: `config.yaml` neben dem Script).

## Ablauf (Phasen)

Ein Lauf fuehrt mehrere Phasen aus (Reihenfolge ist wichtig, damit auch geteilte
Ordner, manuell abgelegte Dateien und Loeschungen fuer **alle** Nutzer korrekt
sind):

S. **Share-Sync** *(nur wenn `external_root` gesetzt und kein `--library`)* –
   geteilte Alben in Permutations-Ordner externalisieren (siehe unten). Laeuft
   zuerst, weil sie die Ordner + Libraries anlegt, die die Phasen 2-4 dann
   verarbeiten.
0. **Loeschungen propagieren** – Loescht ein Nutzer ein externes Album-Bild
   (→ Papierkorb), wird dessen **Datei** im Ordner geloescht und das Asset bei
   allen Nutzern endgueltig entfernt („einmal loeschen = ueberall weg"),
   unwiderruflich.
1. **Uploads → Ordner** – Im Album ergaenzte Uploads in den passenden
   Unterordner der jeweiligen Library kopieren (deterministischer Dateiname,
   keine Duplikate).
2. **Scan & Warten** – Alle Libraries scannen und warten, bis der Import durch
   ist (faengt auch manuell in den Ordner gelegte Dateien ab).
3. **Ordner → Alben** – Alle Library-Assets in die (ggf. neu angelegten)
   Unterordner-Alben aufnehmen, so sehen alle Nutzer denselben Ordnerinhalt.
4. **Aufraeumen** – Die erfolgreich importierten Uploads endgueltig loeschen
   (kein Papierkorb).
5. **Leere Alben entfernen** – Alben, die durch die Loeschphasen von >0 auf 0
   Bilder gefallen sind (also vom Script leergeraeumt wurden), werden geloescht.
   Bereits vorher leere Alben bleiben unangetastet.

## Share-Sync (Phase S)

Teilt ein Nutzer sein Album (normale Immich-Freigabe) mit anderen, sollen **alle
Beteiligten die Bilder gleichberechtigt als eigene External-Assets** bekommen –
statt des Immich-„Owner + Gast"-Modells, bei dem die Bilder weiter dem Ersteller
gehoeren.

Das nutzt aus, dass **mehrere Nutzer je eine eigene External Library auf
denselben Ordner** legen koennen: jeder scannt unabhaengig und besitzt seine
eigene Kopie derselben Dateien.

### Ablauf

Alice teilt „Sommer 2026" mit Bob und Carol ⇒ Gruppe `{Alice, Bob, Carol}`:

1. Die Bilder werden nach `external/<hash der Gruppe>/Sommer 2026/` **verschoben**
   (Originale von Alice werden nach dem Import endgueltig geloescht).
2. Jedes Gruppenmitglied bekommt eine External Library auf `external/<hash>/`.
3. Die **native Immich-Freigabe wird aufgeloest** (Modell B) – sonst saehen die
   anderen das Album doppelt (als Gast **und** als eigene External-Kopie), und es
   entstuenden Namens-Kollisionen. Die Freigabe war nur der Ausloeser.
4. Die normale Ordner→Album-Engine (Phase 3) legt fuer **jeden** ein eigenes,
   ihm gehoerendes Album „Sommer 2026" an.

### Mitgliedschaft ist append-only

Es kann nur jemand **dazukommen**, nie entfernt werden. Da die native Freigabe
nach dem Verarbeiten aufgeloest wird, fuegt man weitere Leute hinzu, indem man
das (jetzt external-basierte) Album **erneut kurz teilt** – der naechste Lauf
erkennt die groessere Gruppe. Waechst sie (`{A,B}` → `{A,B,C}`), zieht der
Album-Ordner in den groesseren Permutations-Ordner um; das neue Mitglied bekommt
seine Library, die anderen ziehen mit. **Permutations-Ordner und Libraries werden
nie geloescht** (auch leer nicht) und bei exakt gleicher Gruppe wiederverwendet.

Neue **Bilder** (statt Nutzer) fuegt jeder einfach seinem eigenen External-Album
hinzu; Phase 1 kopiert sie in den gemeinsamen Ordner, sodass sie bei allen
Mitgliedern ankommen – dafuer ist kein erneutes Teilen noetig.

### Zustand / Manifest

Der Zustand liegt als JSON in `external/.immich_groups.json` (welches Album zu
welcher Gruppe/welchem Ordner gehoert). Daran erkennt das Script bei jedem Lauf,
ob eine Gruppe gewachsen ist. JSON statt YAML, damit keine Zusatz-Abhaengigkeit
noetig ist.

### Umbenennen

Benennt **irgendein** Mitglied sein Album um, zieht das Script das nach: Das
Manifest trackt pro geteiltem Album die Album-ID **jedes** Mitglieds
(`member_albums`, gefuellt nach Phase 3). Beim naechsten Lauf vergleicht
`reconcile_album_renames` die aktuellen Namen aller dieser Alben mit dem
Ordnernamen; weicht einer ab, gilt er als neuer Name → der Disk-Ordner wird
umbenannt und **alle** Mitglieder-Alben werden per `PATCH` auf den neuen Namen
gesetzt. Benennen zwei Mitglieder gleichzeitig unterschiedlich um, gewinnt
deterministisch der alphabetisch erste Name (mit Warnung).

Umbenennung schlaegt also erst beim **naechsten Lauf** in den anderen Konten
durch (nicht sofort).

### Bekannte Eigenheiten

- Die native Freigabe wird nach dem Externalisieren **entfernt** – der
  urspruenglich geteilte Nutzer verliert also die Gast-Ansicht und hat
  stattdessen sein eigenes, ihm gehoerendes External-Album.
- Beim Gruppen-Umzug bleiben die alten (nun leeren) Ordner und die zugehoerigen
  Alben der bisherigen Mitglieder bestehen; Immich verschiebt die verwaisten
  Assets beim Rescan in den Papierkorb.
- Nur Alben von Nutzern mit hinterlegtem API-Key (`user_api_keys` bzw. Admin)
  werden erfasst.
- Ein gezielter `--library`-Lauf ueberspringt Phase S.
