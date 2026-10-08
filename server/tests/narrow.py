
'Narrow an optional helper result at a test call site, loudly.\n\nThe assert earns its place twice: it tells the checker the value is present, and when a\nhelper does return None the failure names that, rather than surfacing as a confusing\nTypeError several lines later.'
def notnone[T](value: T | None) -> T:
    assert value is not None, "helper returned None where this test requires a value"
    return value


def aslist(value):
    'aslist.'
    assert isinstance(value, list), \
        f"expected a list, got {type(value).__name__}: {value!r}"
    return value
