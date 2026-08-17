"""
data/firebase_guard.py – Metering + hard-stop wrappers for Firestore & Storage.

Every module that talks to Firestore imports the `_db` / `_storage_bucket`
handles from data/store.py. By wrapping those two handles here, the whole bot
is metered and gated in one place — no need to touch the 16 call-site modules.

The wrappers are recursive, transparent proxies:

  • On the happy path (Firebase under budget) they pass straight through, only
    incrementing the usage counters in cost_guard so spend can be estimated.
  • Once the Firebase budget is spent, `guard.require_firebase()` raises
    `FirebaseBudgetExceeded` on every network operation (get/set/delete/commit,
    blob upload/download) — the "hard stop".

Counting is best-effort and must never raise; the gate is authoritative.
"""

import logging

from cost_guard import guard

log = logging.getLogger(__name__)

# Reference/query objects we recurse into so a whole `.collection().document()`
# chain stays guarded. Data objects (DocumentSnapshot, query results) are NOT
# wrapped — they are returned to the caller untouched.
_WRAP_SUFFIXES = ("Reference", "Query", "CollectionGroup")
_WRAP_NAMES = {"WriteBatch", "Client", "Transaction"}

# Terminal methods that hit the network → must be gated.
_READ = {"get", "stream"}
_WRITE = {"set", "update", "create", "add"}
_DELETE = {"delete"}
_GATED = _READ | _WRITE | _DELETE | {"commit"}


def _should_wrap(obj) -> bool:
    name = type(obj).__name__
    return name in _WRAP_NAMES or name.endswith(_WRAP_SUFFIXES)


def _maybe_wrap(obj):
    return _GuardedRef(obj) if _should_wrap(obj) else obj


def _unwrap(obj):
    """Return the underlying Firestore object behind a proxy (else obj as-is).

    Critical for calls like `batch.set(doc_ref, data)`: the real WriteBatch does
    isinstance checks on the reference, which a proxy would fail. We unwrap any
    proxy arguments before handing them to the underlying method."""
    return object.__getattribute__(obj, "_obj") if isinstance(obj, _GuardedRef) else obj


def _counting_stream(gen):
    """Wrap a Firestore .stream() generator so each yielded doc counts as a read."""
    count = 0
    try:
        for item in gen:
            count += 1
            yield item
    finally:
        guard.note_firestore(reads=count)


class _GuardedRef:
    """Transparent proxy over a Firestore reference / query / batch / client."""

    __slots__ = ("_obj",)

    def __init__(self, obj):
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name):
        attr = getattr(self._obj, name)
        if not callable(attr):
            return _maybe_wrap(attr)

        def method(*args, **kwargs):
            if name in _GATED:
                guard.require_firebase()  # hard stop once budget is spent
            # Unwrap any proxy args (e.g. a guarded DocumentReference handed to
            # batch.set / transaction.get) so the real lib sees real objects.
            args = tuple(_unwrap(a) for a in args)
            kwargs = {k: _unwrap(v) for k, v in kwargs.items()}
            result = attr(*args, **kwargs)

            # Best-effort metering — never let counting break a real operation.
            try:
                if name == "get":
                    guard.note_firestore(reads=len(result) if isinstance(result, list) else 1)
                elif name == "stream":
                    return _counting_stream(result)
                elif name in _WRITE:
                    guard.note_firestore(writes=1)
                elif name in _DELETE:
                    guard.note_firestore(deletes=1)
                # commit on a WriteBatch is not counted here: each buffered
                # set/update/delete was already counted when it was called.
            except Exception:  # pragma: no cover - metering must not throw
                pass

            # add() returns (update_time, DocumentReference) — keep the ref guarded.
            if name == "add" and isinstance(result, tuple) and len(result) == 2:
                return (result[0], _maybe_wrap(result[1]))
            return _maybe_wrap(result)

        return method

    def __setattr__(self, name, value):
        # See _GuardedBlob.__setattr__: a __slots__-only proxy with no __setattr__
        # turns every attribute write into a confusing AttributeError about the
        # proxy rather than passing it to the wrapped object. Nothing assigns
        # through this proxy today; this is here so nothing has to discover that.
        setattr(self._obj, name, value)

    # Delegate the handful of dunders Firestore objects rely on.
    def __eq__(self, other):
        other = other._obj if isinstance(other, _GuardedRef) else other
        return self._obj == other

    def __hash__(self):
        return hash(self._obj)

    def __repr__(self):
        return f"_GuardedRef({self._obj!r})"


class _GuardedBlob:
    """Proxy over a Storage Blob: gates + meters byte transfers."""

    __slots__ = ("_blob",)

    def __init__(self, blob):
        object.__setattr__(self, "_blob", blob)

    def __getattr__(self, name):
        attr = getattr(self._blob, name)
        if not callable(attr):
            return attr

        def method(*args, **kwargs):
            transfers = name.startswith("upload") or name.startswith("download")
            if transfers or name == "delete":
                guard.require_firebase()
            result = attr(*args, **kwargs)
            try:
                if name.startswith("upload") and args:
                    data = args[0]
                    if isinstance(data, (bytes, bytearray, str)):
                        guard.note_storage(upload=len(data))
                elif name.startswith("download") and isinstance(result, (bytes, bytearray)):
                    guard.note_storage(download=len(result))
            except Exception:  # pragma: no cover
                pass
            return result

        return method

    def __setattr__(self, name, value):
        # Blob metadata (content_disposition, cache_control, content_type, …) is
        # set by plain attribute assignment before the upload. This proxy is
        # __slots__-only and __getattr__ covers reads alone, so without this the
        # write would try to land on the proxy itself and raise AttributeError
        # rather than reach the blob. Nothing is gated or metered here: the write
        # is local, and the network round-trip it prepares (upload_*/patch) is
        # already gated above.
        setattr(self._blob, name, value)


class _GuardedBucket:
    """Proxy over a Storage Bucket so every .blob() is metered/gated."""

    __slots__ = ("_bucket",)

    def __init__(self, bucket):
        object.__setattr__(self, "_bucket", bucket)

    def __getattr__(self, name):
        attr = getattr(self._bucket, name)
        if name == "blob" and callable(attr):
            def blob(*args, **kwargs):
                return _GuardedBlob(attr(*args, **kwargs))
            return blob
        return attr

    def __setattr__(self, name, value):
        # Same reason as _GuardedBlob.__setattr__ — writes must reach the bucket
        # instead of dying on the __slots__-only proxy.
        setattr(self._bucket, name, value)

    def __bool__(self):
        return self._bucket is not None


def wrap_firestore(client):
    """Wrap a firestore client so all access through it is metered and gated."""
    return _GuardedRef(client)


def wrap_bucket(bucket):
    """Wrap a Storage bucket (or return None unchanged)."""
    return _GuardedBucket(bucket) if bucket is not None else None
