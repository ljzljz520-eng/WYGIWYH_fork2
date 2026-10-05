def bump_version(instance, kwargs, field="version"):
    """Bump a monotonic positive-integer version before an existing row is saved.

    Ensures the version column is included even when the caller used
    ``save(update_fields=...)``. No-op for rows that are being inserted.
    """
    if instance._state.adding:
        return

    current = getattr(instance, field) or 1
    setattr(instance, field, current + 1)

    update_fields = kwargs.get("update_fields")
    if update_fields is not None and field not in update_fields:
        kwargs["update_fields"] = tuple(set(update_fields) | {field})
