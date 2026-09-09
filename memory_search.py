"""Rebuildable semantic sidecar index for GenericAgent's file memory.

The files under ``memory/`` remain the source of truth.  This module stores a
rebuildable zvec index under ``temp/memory_zvec_index`` and always reads the
source chunk again before returning it.  If zvec or a remote embedding service
is unavailable, it falls back to a deterministic hashed n-gram index and then
to exact/lexical search.

CLI::

    python3 -m memory_search rebuild [--l4]
    python3 -m memory_search update [--l4]
    python3 -m memory_search search QUERY [--l4] [-k 8]
    python3 -m memory_search exact QUERY [--l4] [-k 8]

Optional OpenAI-compatible embeddings are enabled only when
``GA_MEMORY_EMBEDDING_URL`` is set.  The API key is read from
``GA_MEMORY_EMBEDDING_API_KEY`` but is never persisted or printed.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

try:
    import fcntl  # Linux/macOS; zvec currently targets these platforms.
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


# Version 2 migrates sidecars created before native zvec build/query validation.
# Existing source files remain authoritative, so the sidecar can be rebuilt safely.
INDEX_VERSION = 2
VECTOR_DIMENSION = 384
CHUNK_LINES = 80
SUPPORTED_SUFFIXES = {".md", ".txt", ".py", ".json", ".csv"}


class MemorySearch:
    """Manage a source-verified zvec sidecar index.

    ``memory_dir`` and ``index_dir`` are injectable to make the component easy
    to test and to permit a future project-specific memory root.  The default
    index is deliberately under ``temp/`` so generated files never become
    authoritative memory or source-controlled facts.
    """

    def __init__(
        self,
        memory_dir: str | os.PathLike[str] | None = None,
        index_dir: str | os.PathLike[str] | None = None,
        include_l4: bool = False,
        embedding_timeout: float = 20.0,
    ):
        self.root = Path(__file__).resolve().parent
        self.memory = Path(
            memory_dir or os.environ.get("GA_MEMORY_DIR") or self.root / "memory"
        ).expanduser().resolve()
        self.index = Path(
            index_dir
            or os.environ.get("GA_MEMORY_INDEX_DIR")
            or self.root / "temp" / "memory_zvec_index"
        ).expanduser().resolve()
        self.include_l4 = bool(include_l4)
        self.embedding_timeout = float(embedding_timeout)
        self._zvec = None
        self._last_backend_error = ""
        try:
            import zvec

            self._zvec = zvec
        except ImportError:
            pass

    # ---------- source files and chunks ----------
    def files(self) -> list[Path]:
        if not self.memory.is_dir():
            return []
        result: list[Path] = []
        for path in self.memory.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            # Generated sidecars are never authoritative memory.  Exclude all
            # zvec sidecar trees so a rebuild cannot recursively index itself.
            if any(part in {".zvec_index", "memory_zvec_index"} for part in path.parts):
                continue
            if "__pycache__" in path.parts:
                continue
            if "L4_raw_sessions" in path.parts and not self.include_l4:
                continue
            result.append(path)
        return sorted(result, key=lambda p: self._source_name(p))

    def _source_name(self, path: Path) -> str:
        """Return a portable source id for both project and injected roots."""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            # Tests/integrations may supply a memory root outside the project.
            # Keep the same logical ``memory/...`` namespace without leaking an
            # absolute host path into the index.
            return "memory/" + path.relative_to(self.memory).as_posix()

    def _source_path(self, source: str) -> Path:
        if source == "memory" or source.startswith("memory/"):
            return self.memory / source.removeprefix("memory/")
        return self.root / source

    def _chunks(self, path: Path) -> Iterable[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for start in range(0, len(lines), CHUNK_LINES):
            body = "\n".join(lines[start : start + CHUNK_LINES]).strip()
            if not body:
                continue
            source = self._source_name(path)
            rel_memory = path.relative_to(self.memory).as_posix()
            key = f"{rel_memory}:{start + 1}"
            layer = self._layer_for(source)
            yield {
                "id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:32],
                "key": key,
                "path": source,
                "line": start + 1,
                "text": body,
                "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "layer": layer,
                "kind": self._kind_for(source),
                "session_id": self._session_for(source),
            }

    @staticmethod
    def _layer_for(source: str) -> str:
        if "/L4_raw_sessions/" in f"/{source}/":
            return "L4"
        if source.endswith("/global_mem.txt") or source.endswith("/global_mem_insight.txt"):
            return "L2" if source.endswith("global_mem.txt") else "L1"
        return "L3"

    @staticmethod
    def _kind_for(source: str) -> str:
        if "/L4_raw_sessions/" in f"/{source}/":
            return "session"
        if source.endswith(("_sop.md", "_sop.py")) or "/memory/" in source:
            return "sop"
        return "fact"

    @staticmethod
    def _session_for(source: str) -> str:
        if "/L4_raw_sessions/" not in f"/{source}/":
            return ""
        tail = source.split("/L4_raw_sessions/", 1)[1]
        return tail.split("/", 1)[0]

    def _manifest(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for path in self.files():
            try:
                data = path.read_bytes()
                stat = path.stat()
            except OSError:
                continue
            result[self._source_name(path)] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        return result

    def _all_docs(self) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        for path in self.files():
            docs.extend(self._chunks(path))
        return docs

    # ---------- embeddings ----------
    @staticmethod
    def _terms(text: str) -> list[str]:
        return re.findall(r"[\w\u4e00-\u9fff]+", text.lower())

    @classmethod
    def _features(cls, text: str) -> list[tuple[str, float]]:
        features: list[tuple[str, float]] = []
        for term in cls._terms(text):
            features.append(("t:" + term, 2.0))
            # Character n-grams make Chinese and short technical identifiers
            # searchable without requiring a tokenizer or downloaded model.
            if len(term) >= 2:
                for n in (2, 3, 4):
                    for i in range(max(0, len(term) - n + 1)):
                        features.append((f"g{n}:" + term[i : i + n], 1.0))
        return features

    @classmethod
    def _hash_vector(cls, text: str) -> list[float]:
        vector = [0.0] * VECTOR_DIMENSION
        for feature, weight in cls._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % VECTOR_DIMENSION
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign * weight
        norm = math.sqrt(sum(x * x for x in vector))
        if norm:
            vector = [x / norm for x in vector]
        return vector

    @staticmethod
    def _remote_configured() -> bool:
        return bool(os.environ.get("GA_MEMORY_EMBEDDING_URL", "").strip())

    @staticmethod
    def _config_id() -> str:
        url = os.environ.get("GA_MEMORY_EMBEDDING_URL", "").strip()
        model = os.environ.get("GA_MEMORY_EMBEDDING_MODEL", "").strip()
        if not url:
            return "hash-v1"
        # Do not persist a URL or an API key; only use a stable opaque id.
        return "remote-" + hashlib.sha256(f"{url}\0{model}".encode()).hexdigest()[:16]

    @staticmethod
    def _project_vector(values: list[float]) -> list[float]:
        """Project any remote dimension into the fixed sidecar dimension."""
        result = [0.0] * VECTOR_DIMENSION
        for i, value in enumerate(values):
            digest = hashlib.blake2b(f"projection:{i}".encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % VECTOR_DIMENSION
            result[bucket] += float(value) * (1.0 if digest[4] & 1 else -1.0)
        norm = math.sqrt(sum(x * x for x in result))
        return [x / norm for x in result] if norm else result

    def _remote_vector(self, text: str) -> list[float]:
        url = os.environ["GA_MEMORY_EMBEDDING_URL"].strip().rstrip("/")
        if not url.endswith("/embeddings"):
            url += "/embeddings"
        payload: dict[str, Any] = {"input": text}
        model = os.environ.get("GA_MEMORY_EMBEDDING_MODEL", "").strip()
        if model:
            payload["model"] = model
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **(
                    {"Authorization": "Bearer " + os.environ["GA_MEMORY_EMBEDDING_API_KEY"]}
                    if os.environ.get("GA_MEMORY_EMBEDDING_API_KEY")
                    else {}
                ),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.embedding_timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        values = data.get("data", [{}])[0].get("embedding")
        if not isinstance(values, list) or not values:
            raise ValueError("embedding response has no data[0].embedding")
        return self._project_vector([float(value) for value in values])

    def _vector(self, text: str, backend: str = "hash") -> list[float]:
        if backend == "remote":
            return self._remote_vector(text)
        return self._hash_vector(text)

    def _choose_backend(self, docs: list[dict[str, Any]]) -> tuple[str, list[list[float]]]:
        if not docs or not self._remote_configured():
            return "hash", [self._hash_vector(d["text"]) for d in docs]
        try:
            vectors = [self._remote_vector(d["text"]) for d in docs]
            return "remote", vectors
        except Exception as exc:
            # Remote embeddings are an enhancement, never a prerequisite for
            # local memory search. Keep the reason internal and secret-free.
            self._last_backend_error = type(exc).__name__
            return "hash", [self._hash_vector(d["text"]) for d in docs]

    # ---------- persistence and locking ----------
    @contextlib.contextmanager
    def _write_lock(self):
        self.index.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.index.parent / (self.index.name + ".lock")
        with lock_path.open("a+") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return default

    def _meta(self) -> dict[str, Any]:
        return self._read_json(self.index / "meta.json", {})

    def _schema(self):
        zvec = self._zvec
        return zvec.CollectionSchema(
            name="ga_memory",
            fields=[
                zvec.FieldSchema("path", zvec.DataType.STRING),
                zvec.FieldSchema("layer", zvec.DataType.STRING),
                zvec.FieldSchema("kind", zvec.DataType.STRING),
                zvec.FieldSchema("session_id", zvec.DataType.STRING),
                zvec.FieldSchema("line", zvec.DataType.INT32),
                zvec.FieldSchema("text", zvec.DataType.STRING),
                zvec.FieldSchema("content_hash", zvec.DataType.STRING),
            ],
            vectors=zvec.VectorSchema(
                "embedding",
                zvec.DataType.VECTOR_FP32,
                VECTOR_DIMENSION,
                zvec.FlatIndexParam(),
            ),
        )

    def _open_collection(self, read_only: bool):
        if self._zvec is None or not self.index.exists():
            return None
        option = self._zvec.CollectionOption(
            read_only=read_only,
            enable_mmap=read_only,
        )
        return self._zvec.open(str(self.index), option=option)

    def _zvec_docs(self, docs: list[dict[str, Any]], vectors: list[list[float]]):
        collection = self._zvec.create_and_open(str(self.index), schema=self._schema())
        try:
            records = []
            for doc, vector in zip(docs, vectors):
                fields = {k: doc[k] for k in ("path", "layer", "kind", "session_id", "line", "text", "content_hash")}
                records.append(self._zvec.Doc(id=doc["id"], fields=fields, vectors={"embedding": vector}))
            if records:
                statuses = collection.insert(records)
                if isinstance(statuses, list) and any(not status.ok() for status in statuses):
                    raise RuntimeError("zvec insert returned a failed status")
            collection.flush()
        finally:
            collection.close()

    def _write_meta(self, manifest: dict[str, Any], backend: str) -> None:
        self._atomic_json(
            self.index / "meta.json",
            {
                "version": INDEX_VERSION,
                "dimension": VECTOR_DIMENSION,
                "backend": backend,
                "requested_remote": self._remote_configured(),
                "config_id": self._config_id(),
                "include_l4": self.include_l4,
                "manifest": manifest,
            },
        )

    def _rebuild_locked(self) -> int:
        docs = self._all_docs()
        manifest = self._manifest()
        backend, vectors = self._choose_backend(docs)

        # The sidecar is generated data. Replacing it gives rebuild a clean,
        # deterministic schema and leaves source memory untouched.
        if self.index.exists():
            import shutil

            shutil.rmtree(self.index)
        self.index.parent.mkdir(parents=True, exist_ok=True)
        zvec_written = False
        if self._zvec is not None:
            try:
                self._zvec_docs(docs, vectors)
                zvec_written = True
            except Exception as exc:
                self._last_backend_error = type(exc).__name__
                if self.index.exists():
                    import shutil

                    shutil.rmtree(self.index)
        self.index.mkdir(parents=True, exist_ok=True)
        self._atomic_json(self.index / "documents.json", docs)
        self._atomic_json(self.index / "manifest.json", manifest)
        self._write_meta(manifest, backend)
        # This flag makes a broken native index explicit and lets search safely
        # use the JSON lexical fallback without losing the source manifest.
        meta = self._meta()
        meta["zvec"] = zvec_written
        self._atomic_json(self.index / "meta.json", meta)
        return len(docs)

    def rebuild(self) -> int:
        with self._write_lock():
            return self._rebuild_locked()

    def _configuration_changed(self, meta: dict[str, Any]) -> bool:
        if not meta or meta.get("version") != INDEX_VERSION:
            return True
        if bool(meta.get("include_l4")) != self.include_l4:
            return True
        # If zvec has become available since a lexical-only rebuild, upgrade the
        # disposable sidecar on the next update.  Source files remain intact.
        if self._zvec is not None and not bool(meta.get("zvec")):
            return True
        return meta.get("backend", "hash") not in {"hash", "remote"}

    def _incremental_locked(self, old_manifest: dict[str, Any], manifest: dict[str, Any], meta: dict[str, Any]) -> int:
        docs = self._read_json(self.index / "documents.json", [])
        if not isinstance(docs, list):
            return self._rebuild_locked()
        old_by_path: dict[str, list[dict[str, Any]]] = {}
        for doc in docs:
            if isinstance(doc, dict):
                old_by_path.setdefault(str(doc.get("path", "")), []).append(doc)
        changed = {p for p in set(old_manifest) | set(manifest) if old_manifest.get(p) != manifest.get(p)}
        if not changed:
            return 0
        backend = str(meta.get("backend", "hash"))
        replacement_docs: list[dict[str, Any]] = []
        for source in sorted(changed):
            if source not in manifest:
                continue
            path = self._source_path(source)
            if path.exists():
                replacement_docs.extend(self._chunks(path))
        replacement_vectors: list[list[float]] = []
        try:
            replacement_vectors = [self._vector(d["text"], backend) for d in replacement_docs]
        except Exception:
            # A remote service can disappear between calls. Rebuild entirely
            # with the deterministic backend rather than mixing vector spaces.
            return self._rebuild_locked_with_backend("hash")

        old_changed = [d for source in changed for d in old_by_path.get(source, [])]
        zvec_written = bool(meta.get("zvec")) and self._zvec is not None
        if zvec_written:
            collection = self._open_collection(read_only=False)
            try:
                old_ids = [d["id"] for d in old_changed]
                if old_ids:
                    collection.delete(old_ids)
                if replacement_docs:
                    collection.upsert([
                        self._zvec.Doc(
                            id=d["id"],
                            fields={k: d[k] for k in ("path", "layer", "kind", "session_id", "line", "text", "content_hash")},
                            vectors={"embedding": vector},
                        )
                        for d, vector in zip(replacement_docs, replacement_vectors)
                    ])
                collection.flush()
            except Exception as exc:
                self._last_backend_error = f"incremental zvec update: {exc!r}"
                try:
                    collection.close()
                finally:
                    return self._rebuild_locked_with_backend("hash")
            finally:
                try:
                    collection.close()
                except Exception:
                    pass

        retained = [d for d in docs if d.get("path") not in changed]
        merged = retained + replacement_docs
        merged.sort(key=lambda d: d.get("key", ""))
        self._atomic_json(self.index / "documents.json", merged)
        self._atomic_json(self.index / "manifest.json", manifest)
        self._write_meta(manifest, backend)
        meta = self._meta()
        meta["zvec"] = zvec_written
        self._atomic_json(self.index / "meta.json", meta)
        return len(replacement_docs)

    def _rebuild_locked_with_backend(self, backend: str) -> int:
        # Used only after a live embedding/index failure. It avoids silently
        # combining remote and hash vectors in one collection.
        docs = self._all_docs()
        manifest = self._manifest()
        vectors = [self._vector(d["text"], backend) for d in docs]
        if self.index.exists():
            import shutil

            shutil.rmtree(self.index)
        self.index.parent.mkdir(parents=True, exist_ok=True)
        zvec_written = False
        if self._zvec is not None:
            try:
                self._zvec_docs(docs, vectors)
                zvec_written = True
            except Exception:
                if self.index.exists():
                    import shutil

                    shutil.rmtree(self.index)
        self.index.mkdir(parents=True, exist_ok=True)
        self._atomic_json(self.index / "documents.json", docs)
        self._atomic_json(self.index / "manifest.json", manifest)
        self._write_meta(manifest, backend)
        meta = self._meta(); meta["zvec"] = zvec_written
        self._atomic_json(self.index / "meta.json", meta)
        return len(docs)

    def update(self) -> int:
        with self._write_lock():
            manifest = self._manifest()
            old_manifest = self._read_json(self.index / "manifest.json", None)
            meta = self._meta()
            if old_manifest is None or not (self.index / "documents.json").exists() or self._configuration_changed(meta):
                return self._rebuild_locked()
            return self._incremental_locked(old_manifest, manifest, meta)

    # ---------- verified retrieval ----------
    def _verified(self, doc: dict[str, Any]) -> dict[str, Any]:
        source = str(doc.get("path", ""))
        path = self._source_path(source)
        result = {
            "score": float(doc.get("score", 0.0)),
            "path": source,
            "line": int(doc.get("line", 1)),
            "layer": doc.get("layer", self._layer_for(source)),
            "kind": doc.get("kind", self._kind_for(source)),
            "session_id": doc.get("session_id", self._session_for(source)),
            "text": "",
            "verified": False,
            "backend": doc.get("backend", ""),
        }
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = result["line"]
            current = "\n".join(lines[start - 1 : start - 1 + CHUNK_LINES]).strip()
            result["text"] = current
            result["verified"] = hashlib.sha256(current.encode("utf-8")).hexdigest() == doc.get("content_hash")
        except OSError:
            pass
        return result

    @staticmethod
    def _allowed_layers(include_l4: bool, layers: Iterable[str] | None) -> set[str]:
        if layers is None:
            return {"L1", "L2", "L3", "L4"} if include_l4 else {"L1", "L2", "L3"}
        return {str(layer).upper() for layer in layers}

    def _lexical(self, query: str, docs: list[dict[str, Any]], k: int, layers: set[str]) -> list[dict[str, Any]]:
        q = set(self._terms(query))
        scored: list[tuple[float, dict[str, Any]]] = []
        for doc in docs:
            if doc.get("layer") not in layers:
                continue
            terms = set(self._terms(str(doc.get("text", "")) + " " + str(doc.get("path", ""))))
            overlap = len(q & terms)
            if overlap:
                scored.append((overlap / max(1, len(q)), doc))
        scored.sort(key=lambda item: (-item[0], item[1].get("key", "")))
        return [self._verified({**doc, "score": score, "backend": "lexical-fallback"}) for score, doc in scored[:k]]

    def search(self, query: str, k: int = 8, layers: Iterable[str] | None = None) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        k = max(1, min(int(k), 100))
        self.update()
        docs = self._read_json(self.index / "documents.json", [])
        allowed = self._allowed_layers(self.include_l4, layers)
        meta = self._meta()
        if bool(meta.get("zvec")) and self._zvec is not None:
            collection = None
            try:
                collection = self._open_collection(read_only=True)
                backend = str(meta.get("backend", "hash"))
                vector = self._vector(query, backend)
                # zvec filter grammar differs across releases (0.7.0 rejects
                # the SQL-style equality used by older examples).  Retrieve a
                # bounded candidate window and apply the authoritative layer
                # constraint in Python instead of silently returning another
                # layer's memories.
                candidate_k = min(100, max(k * 4, k))
                hits = collection.query(
                    self._zvec.Query(field_name="embedding", vector=vector),
                    topk=candidate_k,
                    output_fields=["path", "layer", "kind", "session_id", "line", "text", "content_hash"],
                )
                result = []
                for hit in hits:
                    if str(hit.fields.get("layer", "")).upper() not in allowed:
                        continue
                    result.append(self._verified({**hit.fields, "score": hit.score, "backend": "zvec"}))
                    if len(result) >= k:
                        break
                return result
            except Exception:
                # Search must remain useful if a sidecar is stale/corrupt.
                pass
            finally:
                if collection is not None:
                    try:
                        collection.close()
                    except Exception:
                        pass
        return self._lexical(query, docs if isinstance(docs, list) else [], k, allowed)

    def exact(self, query: str, k: int = 8, layers: Iterable[str] | None = None) -> list[dict[str, Any]]:
        query = (query or "").strip().lower()
        if not query:
            return []
        self.update()
        allowed = self._allowed_layers(self.include_l4, layers)
        docs = self._read_json(self.index / "documents.json", [])
        result = []
        for doc in docs if isinstance(docs, list) else []:
            if doc.get("layer") not in allowed:
                continue
            if query in str(doc.get("text", "")).lower() or query in str(doc.get("path", "")).lower():
                result.append(self._verified({**doc, "score": 1.0, "backend": "exact"}))
                if len(result) >= k:
                    break
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Search GenericAgent's verified file memory")
    parser.add_argument("action", choices=["rebuild", "update", "search", "exact"])
    parser.add_argument("query", nargs="?")
    parser.add_argument("--l4", action="store_true", help="include raw L4 sessions")
    parser.add_argument("--layer", action="append", dest="layers", help="restrict to a layer; repeatable")
    parser.add_argument("-k", type=int, default=8)
    args = parser.parse_args()
    searcher = MemorySearch(include_l4=args.l4)
    if args.action == "rebuild":
        output: Any = {"chunks": searcher.rebuild()}
    elif args.action == "update":
        output = {"chunks_replaced": searcher.update()}
    elif args.action == "search":
        output = searcher.search(args.query or "", args.k, args.layers)
    else:
        output = searcher.exact(args.query or "", args.k, args.layers)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
