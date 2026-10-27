"""Zotero sources share one metadata/annotation/PDF preparation pipeline."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class ZoteroPaper:
    pdf_path: Path
    metadata: dict[str, Any]
    annotations: list[dict[str, Any]]
    provenance: dict[str, Any]
    fingerprint: str


class ZoteroClient:
    pdf_link_modes = {"imported_file", "imported_url"}

    def __init__(self, library_id: str, *, api_key: str = "", library_type: str = "users",
                 timeout: float = 60, transport: httpx.BaseTransport | None = None):
        if library_type not in {"users", "groups"} or not str(library_id).isdigit():
            raise ValueError("Zotero requires a numeric library ID and users/groups library type")
        self.library_id = str(library_id)
        self.library_type = library_type
        self.base = f"https://api.zotero.org/{library_type}/{library_id}"
        self.http = httpx.Client(timeout=timeout, transport=transport)
        self.headers = {"Zotero-API-Version": "3"}
        if api_key:
            self.headers["Zotero-API-Key"] = api_key

    def close(self) -> None:
        self.http.close()

    @staticmethod
    def key(value: str) -> str:
        if not re.fullmatch(r"[A-Z0-9]{8}", value):
            raise ValueError("Zotero item/collection keys must be 8 uppercase letters or digits")
        return value

    def _get(self, path: str, **params: Any) -> httpx.Response:
        response = self.http.get(self.base + path, headers=self.headers, params=params)
        if response.status_code >= 400:
            raise RuntimeError(f"Zotero API returned HTTP {response.status_code} for {path}")
        return response

    def _list(self, path: str) -> list[dict[str, Any]]:
        records = []
        start = 0
        while True:
            batch = self._get(path, start=start, limit=100, format="json").json()
            if not isinstance(batch, list):
                raise ValueError("Zotero API expected a list of items")
            records.extend(batch)
            if len(batch) < 100:
                return records
            start += len(batch)

    def collection_items(self, collection_key: str) -> list[dict[str, Any]]:
        return [item for item in self._list(f"/collections/{self.key(collection_key)}/items/top")
                if item["data"].get("itemType") not in {"attachment", "note", "annotation"}]

    def item(self, item_key: str) -> dict[str, Any]:
        return self._get(f"/items/{self.key(item_key)}").json()

    def children(self, item_key: str) -> list[dict[str, Any]]:
        return self._list(f"/items/{self.key(item_key)}/children")

    def _pdf_content(self, attachment: dict[str, Any]) -> bytes:
        response = self._get(f"/items/{self.key(attachment['key'])}/file")
        if response.is_redirect:
            location = response.headers.get("location", "")
            if not location.startswith("https://"):
                raise RuntimeError("Zotero file redirect must use HTTPS")
            # Never forward the Zotero API key to the file storage host.
            response = self.http.get(location, follow_redirects=True)
            if response.status_code >= 400:
                raise RuntimeError(f"Zotero PDF download returned HTTP {response.status_code}")
        return response.content

    def prepare(self, project: Path, item_key: str) -> ZoteroPaper:
        item_key = self.key(item_key)
        item = self.item(item_key)
        data = item["data"]
        if data.get("itemType") in {"attachment", "note", "annotation"}:
            raise ValueError("Select the parent bibliographic item, not its attachment/note")
        attachments = [record for record in self.children(item_key)
                       if record["data"].get("itemType") == "attachment"
                       and record["data"].get("contentType") == "application/pdf"
                       and record["data"].get("linkMode") in self.pdf_link_modes]
        if not attachments:
            raise ValueError(f"{item_key}: no supported PDF attachment")
        attachments.sort(key=lambda record: ("supp" in record["data"].get("title", "").lower(), record["key"]))
        attachment = attachments[0]
        attachment_key = self.key(attachment["key"])
        content = self._pdf_content(attachment)
        if not content.startswith(b"%PDF-"):
            raise ValueError(f"{item_key}: downloaded attachment is not a PDF")
        target = (project / "papers" / "zotero" / f"{self.library_type}-{self.library_id}"
                  / item_key / f"{attachment_key}.pdf").resolve()
        if not target.is_relative_to(project.resolve()):
            raise ValueError("Zotero cache path escapes workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        annotations = []
        for record in self.children(attachment_key):
            note = record["data"]
            if note.get("itemType") == "annotation":
                annotations.append({"key": record["key"], "type": note.get("annotationType"),
                                    "text": note.get("annotationText", ""),
                                    "comment": note.get("annotationComment", ""),
                                    "page_label": note.get("annotationPageLabel", ""),
                                    "position": note.get("annotationPosition", "")})
        annotations.sort(key=lambda note: note["key"])
        authors = [creator.get("name") or " ".join(filter(None, [creator.get("firstName"), creator.get("lastName")]))
                   for creator in data.get("creators", []) if creator.get("creatorType") == "author"]
        year_match = re.search(r"\b(18|19|20|21)\d{2}\b", data.get("date", ""))
        metadata = {"title": data.get("title", ""), "authors": [a for a in authors if a],
                    "year": int(year_match.group()) if year_match else None,
                    "doi": data.get("DOI", ""),
                    "venue": data.get("publicationTitle") or data.get("conferenceName", "")}
        provenance = {"provider": "zotero", "library_type": self.library_type,
                      "library_id": self.library_id, "item_key": item_key,
                      "attachment_key": attachment_key, "item_version": item.get("version"),
                      "pdf_sha256": hashlib.sha256(content).hexdigest(),
                      "tags": data.get("tags", []), "collections": data.get("collections", [])}
        fingerprint = hashlib.sha256(json.dumps(
            {"pdf": provenance["pdf_sha256"], "metadata": metadata, "annotations": annotations},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return ZoteroPaper(target, metadata, annotations, provenance, fingerprint)


class LocalZoteroClient(ZoteroClient):
    """Read a snapshot of the current Zotero personal-library database, never modify it."""

    pdf_link_modes = {"imported_file", "imported_url", "linked_file"}

    def __init__(self, library_id: str, data_dir: str | Path):
        if not str(library_id).isdigit():
            raise ValueError("Zotero user ID must be numeric")
        self.library_id = str(library_id)
        self.library_type = "users"
        self.data_dir = Path(data_dir).expanduser().resolve()
        database = self.data_dir / "zotero.sqlite"
        if not database.is_file():
            raise FileNotFoundError(f"Zotero database not found: {database}")
        self.db = sqlite3.connect(":memory:")
        try:
            # SQLite backup includes committed WAL data and gives the batch a consistent view.
            source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
            try:
                source.backup(self.db)
            finally:
                source.close()
            self.db.row_factory = sqlite3.Row
            row = self.db.execute("SELECT libraryID FROM libraries WHERE type='user'").fetchone()
            if row is None:
                raise ValueError("Zotero database has no personal library")
            self.local_library_id = row[0]
        except Exception:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def _record(self, row: sqlite3.Row) -> dict[str, Any]:
        item_id = row["itemID"]
        data = dict(self.db.execute(
            "SELECT f.fieldName, v.value FROM itemData d "
            "JOIN fieldsCombined f USING(fieldID) JOIN itemDataValues v USING(valueID) "
            "WHERE d.itemID=?", (item_id,)).fetchall())
        data["itemType"] = row["typeName"]
        data["creators"] = [dict(creator) for creator in self.db.execute(
            "SELECT c.firstName, c.lastName, t.creatorType FROM itemCreators ic "
            "JOIN creators c USING(creatorID) JOIN creatorTypes t USING(creatorTypeID) "
            "WHERE ic.itemID=? ORDER BY ic.orderIndex", (item_id,))]
        attachment = self.db.execute("SELECT * FROM itemAttachments WHERE itemID=?", (item_id,)).fetchone()
        if attachment:
            data.update({"contentType": attachment["contentType"], "path": attachment["path"],
                         "linkMode": {0: "imported_file", 1: "imported_url", 2: "linked_file", 3: "linked_url"}.get(attachment["linkMode"])})
        annotation = self.db.execute("SELECT * FROM itemAnnotations WHERE itemID=?", (item_id,)).fetchone()
        if annotation:
            data.update({"annotationType": {1: "highlight", 2: "note", 3: "image", 4: "underline", 5: "text", 6: "ink"}.get(annotation["type"], str(annotation["type"])),
                         "annotationText": annotation["text"] or "",
                         "annotationComment": annotation["comment"] or "",
                         "annotationPageLabel": annotation["pageLabel"] or "",
                         "annotationPosition": annotation["position"]})
        return {"key": row["key"], "version": row["version"], "data": data}

    def _rows(self, where: str, params: tuple) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT i.*, t.typeName FROM items i JOIN itemTypesCombined t USING(itemTypeID) "
            "WHERE i.libraryID=? AND i.itemID NOT IN (SELECT itemID FROM deletedItems) AND "
            + where + " ORDER BY i.key", (self.local_library_id, *params)).fetchall()

    def item(self, item_key: str) -> dict[str, Any]:
        rows = self._rows("i.key=?", (self.key(item_key),))
        if not rows:
            raise ValueError(f"Zotero item not found in local personal library: {item_key}")
        return self._record(rows[0])

    def children(self, item_key: str) -> list[dict[str, Any]]:
        parent = self._rows("i.key=?", (self.key(item_key),))
        if not parent:
            raise ValueError(f"Zotero item not found: {item_key}")
        item_id = parent[0]["itemID"]
        rows = self._rows(
            "(i.itemID IN (SELECT itemID FROM itemAttachments WHERE parentItemID=?) "
            "OR i.itemID IN (SELECT itemID FROM itemAnnotations WHERE parentItemID=?))",
            (item_id, item_id))
        return [self._record(row) for row in rows]

    def collection_items(self, collection_key: str) -> list[dict[str, Any]]:
        collection = self.db.execute(
            "SELECT collectionID FROM collections WHERE libraryID=? AND key=?",
            (self.local_library_id, self.key(collection_key))).fetchone()
        if collection is None:
            raise ValueError(f"Local Zotero collection not found: {collection_key}")
        return [self._record(row) for row in self._rows(
            "i.itemID IN (SELECT itemID FROM collectionItems WHERE collectionID=?) "
            "AND t.typeName NOT IN ('attachment', 'note', 'annotation')", (collection[0],))]

    def _pdf_content(self, attachment: dict[str, Any]) -> bytes:
        data = attachment["data"]
        stored_path = data.get("path") or ""
        if stored_path.startswith("storage:"):
            directory = self.data_dir / "storage" / self.key(attachment["key"])
            path = (directory / stored_path.removeprefix("storage:")).resolve()
            if not path.is_relative_to(directory.resolve()):
                raise ValueError("Zotero attachment path escapes its storage directory")
        elif data.get("linkMode") == "linked_file" and Path(stored_path).is_absolute():
            path = Path(stored_path)
        else:
            raise ValueError("Local PDF requires a storage: path or an absolute linked-file path")
        return path.read_bytes()
