"""test_suspensions.py – Behavioural tests for the service-suspension gate.

No network, no Firebase, no Discord: `data.store` is stubbed out before import so
`data/suspensions.py` runs against a fake Firestore collection. The module under
test is the shipped one, not a re-implementation.

What is covered:
  [A] the basic contract    suspend → active, expiry → not active
  [B] the cache             writes take effect at once; reads are served from it
  [C] the expiry clamp      a cached entry never outlives the suspension
  [D] lifting               early lift, and lifting nothing
  [E] failing open          a Firestore error is "not suspended", never "suspended"
  [F] bounds                a duration is clamped, never unbounded
  [G] list_active           expired and lifted records stay out of it

Run:  ./.venv/bin/python test_suspensions.py
"""
import os
import sys
import time
import types

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


# ── Fake Firestore ────────────────────────────────────────────────────────────

class FakeSnap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDoc:
    def __init__(self, col, doc_id):
        self.col, self.id = col, doc_id

    def get(self):
        self.col.reads += 1
        if self.col.fail:
            raise RuntimeError("firestore is down")
        return FakeSnap(self.col.docs.get(self.id))

    def set(self, data, merge=False):
        if self.col.fail:
            raise RuntimeError("firestore is down")
        self.col.docs[self.id] = dict(data)


class FakeCol:
    def __init__(self):
        self.docs, self.reads, self.fail = {}, 0, False

    def document(self, doc_id):
        return FakeDoc(self, doc_id)

    def where(self, field, op, value):
        assert op == ">", op
        self._filter = (field, value)
        return self

    def stream(self):
        if self.fail:
            raise RuntimeError("firestore is down")
        field, value = self._filter
        return [FakeSnap(d) for d in self.docs.values() if (d.get(field) or 0) > value]


class FakeDb:
    def __init__(self, col):
        self._col = col

    def collection(self, name):
        assert name == "suspensions", name
        return self._col


COL = FakeCol()
_stub = types.ModuleType("data.store")
_stub._db = FakeDb(COL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data  # noqa: E402  (real package, for the submodule path)
sys.modules["data.store"] = _stub
data.store = _stub

from data import suspensions as S  # noqa: E402


def reset():
    COL.docs.clear()
    COL.reads = 0
    COL.fail = False
    S._cache.clear()


# ── [A] the basic contract ────────────────────────────────────────────────────

print("\n[A] suspend / expire")
reset()
rec = S.suspend("42", 2, "spamming bug reports", "owner")
check("suspend returns a record with an expiry", rec["until"] > time.time())
check("the user is suspended", S.get_active("42") is not None)
check("someone else is not", S.get_active("43") is None)
check("the reason is kept verbatim", S.get_active("42")["reason"] == "spamming bug reports")

# Wind the stored expiry back into the past — the read path resolves expiry, so
# no sweeper and no waiting.
reset()
S.suspend("42", 2, "r", "owner")
COL.docs["42"]["until"] = time.time() - 1
S._cache.clear()
check("an expired suspension is not active", S.get_active("42") is None)
check("but the record survives for the audit trail", S.get_record("42") is not None)


# ── [B] the cache ─────────────────────────────────────────────────────────────

print("\n[B] cache")
reset()
S.suspend("42", 24, "r", "owner")
before = COL.reads
S.get_active("42")
S.get_active("42")
check("a write primes the cache (no read to see its own effect)", COL.reads == before,
      f"{COL.reads - before} reads")

reset()
S.get_active("99")            # miss → one read, negative result cached
first = COL.reads
S.get_active("99")
check("a clean user is cached too (no read per request)", COL.reads == first)


# ── [C] the expiry clamp ──────────────────────────────────────────────────────

print("\n[C] a cached entry never outlives the suspension")
reset()
S.suspend("42", 24, "r", "owner")
# Fake a cache entry written 5 s ago for a suspension that ended 1 s ago. Without
# the clamp the plain 30 s TTL would keep enforcing it for another 25 s.
now = time.time()
S._cache["42"] = ({**COL.docs["42"], "until": now - 1}, now - 5)
COL.docs["42"]["until"] = now - 1
check("expiry beats the TTL", S.get_active("42") is None)


# ── [D] lifting ───────────────────────────────────────────────────────────────

print("\n[D] lift")
reset()
S.suspend("42", 24, "r", "owner")
check("lift reports it did something", S.lift("42", "owner") is True)
check("and the user is free immediately", S.get_active("42") is None)
check("the record says who lifted it", S.get_record("42")["lifted_by"] == "owner")
check("lifting nothing is False, not an error", S.lift("43", "owner") is False)


# ── [E] failing open ──────────────────────────────────────────────────────────

print("\n[E] a broken Firestore does not suspend the world")
reset()
S.suspend("42", 24, "r", "owner")
S._cache.clear()
COL.fail = True
check("an unreadable record is not a suspension", S.get_active("42") is None)
check("the failed read is not cached", "42" not in S._cache)
COL.fail = False
check("and it comes back when Firestore does", S.get_active("42") is not None)

reset()
COL.fail = True
check("list_active survives an outage", S.list_active() == [])


# ── [F] bounds ────────────────────────────────────────────────────────────────

print("\n[F] duration bounds")
reset()
long_one = S.suspend("42", 10 ** 6, "r", "owner")
check("a silly duration is clamped to the ceiling", long_one["hours"] == S.MAX_HOURS)
short = S.suspend("43", 0, "r", "owner")
check("and zero is raised to the floor (never a no-op suspension)",
      short["hours"] == S.MIN_HOURS)
check("there is no unbounded/permanent option", S.MAX_HOURS <= 365 * 24)


# ── [G] list_active ───────────────────────────────────────────────────────────

print("\n[G] list_active")
reset()
S.suspend("1", 24, "a", "owner")
S.suspend("2", 24, "b", "owner")
S.suspend("3", 24, "c", "owner")
S.lift("2", "owner")
COL.docs["3"]["until"] = time.time() - 1          # expired on its own
ids = sorted(r["user_id"] for r in S.list_active())
check("only the ones still running are listed", ids == ["1"], str(ids))


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
