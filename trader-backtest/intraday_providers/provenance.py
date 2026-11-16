"""Guard trigger-to-exit provenance without depending on provider names."""

BASIS_FIELDS = ("storage_basis", "storage_basis_id", "basis_status")


def source_identity(rows, plan=None):
    observations = list(rows)
    if plan and any(plan.get(k) for k in BASIS_FIELDS):
        observations.append(plan)
    marked = [r for r in observations if any(r.get(k) for k in BASIS_FIELDS)]
    basis = {}
    if marked:
        if len(marked) != len(observations) or any(r.get("basis_status") != "VERIFIED" or
                r.get("storage_basis") != "FROZEN_DAILY_SPLIT_BASIS" or not r.get("storage_basis_id") for r in marked):
            return dict(status="BASIS_COMPATIBILITY_UNKNOWN")
        if len({r["storage_basis_id"] for r in marked}) != 1:
            return dict(status="BASIS_INCOMPATIBLE")
        basis = {k: marked[0][k] for k in BASIS_FIELDS}
    keys = ("provider", "feed", "adjustment")
    identities = {tuple(r.get(k) for k in keys) for r in rows}
    expected = tuple((plan or {}).get(k) for k in keys)
    if any(expected):
        identities.add(expected)
    if not identities:
        return dict(status="DATA_UNAVAILABLE")
    if identities == {(None, None, None)}:
        return dict(status="LEGACY_UNSPECIFIED")
    if len(identities) != 1 or any(v is None for identity in identities for v in identity):
        return dict(status="MIXED_PROVIDER_DATA", flags=["MIXED_PROVIDER_DATA"])
    identity = next(iter(identities))
    return dict(zip(keys, identity), status="AVAILABLE", **basis)
