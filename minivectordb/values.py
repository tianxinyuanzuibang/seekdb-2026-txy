"""Value semantics shared by the executor, storage layer and vector code.

Column types are limited to BIGINT, VARCHAR(n) and VECTOR(n) per the contest
statement.  Values are represented as Python objects:

* BIGINT  -> ``int``
* VARCHAR -> ``str``
* VECTOR  -> ``tuple`` of ``float`` (stored values are float32-rounded)

Ordering rules follow the statement: strings compare by Unicode code point
(so plain Python ``str`` comparison is correct) and vectors are never
compared with ``<``.
"""

import math
import struct

from .errors import SQLError, invalid_value

# --- float32 helpers -------------------------------------------------------


def to_float32(value):
    """Round a Python float to the nearest float32 value.

    Raises ``SQLError`` when the value cannot be represented as a finite
    float32 (NaN, Infinity, or overflow).
    """
    if isinstance(value, bool):
        value = int(value)
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise invalid_value("cannot convert {!r} to a number".format(value))
    if math.isnan(f):
        raise invalid_value("NaN is not a valid vector element")
    if math.isinf(f):
        raise invalid_value("Infinity is not a valid vector element")
    # Overflow check: float32 max is ~3.4028235e38.
    if f > 3.4028235677973366e38 or f < -3.4028235677973366e38:
        raise invalid_value(
            "value {} overflows float32 range".format(f)
        )
    packed = struct.unpack("<f", struct.pack("<f", f))[0]
    if math.isnan(packed) or math.isinf(packed):
        raise invalid_value(
            "value {} overflows float32 range".format(f)
        )
    return packed


def to_float32_values(values):
    """Convert a sequence of numbers to float32 in one pass.

    Bulk conversion matters: the benchmark inserts 10,000 rows of
    128-dimensional vectors, and converting element by element costs two
    ``struct`` calls per number.  Packing the whole vector at once is
    roughly an order of magnitude faster.
    """
    if not values:
        return ()
    numbers = []
    for element in values:
        if isinstance(element, bool):
            element = int(element)
        if not isinstance(element, (int, float)):
            raise invalid_value(
                "vector elements must be numeric, got {!r}".format(element)
            )
        numbers.append(float(element))
    packed = struct.pack("<{}f".format(len(numbers)), *numbers)
    result = struct.unpack("<{}f".format(len(numbers)), packed)
    # struct.pack raises OverflowError for values outside the float32 range
    # and packs NaN/Infinity happily, so both are checked explicitly.
    for original, converted in zip(numbers, result):
        if math.isnan(converted) or math.isinf(converted):
            raise invalid_value(
                "value {} overflows float32 range".format(original)
            )
    return result


def is_finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def l2_distance(a, b):
    """Euclidean distance between two float32 vectors.

    ``math.dist`` is a C loop and is roughly 15x faster than a pure Python
    loop for the 128-dimensional vectors used by the benchmark, so it is
    preferred whenever both operands are plain sequences of numbers.
    """
    try:
        return math.dist(a, b)
    except (TypeError, ValueError):
        total = 0.0
        for x, y in zip(a, b):
            d = x - y
            total += d * d
        return math.sqrt(total)


def l2_distance_sq(a, b):
    total = 0.0
    for x, y in zip(a, b):
        d = x - y
        total += d * d
    return total


def cosine_distance(a, b):
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 1.0
    cos = dot / (math.sqrt(na) * math.sqrt(nb))
    if cos > 1.0:
        cos = 1.0
    elif cos < -1.0:
        cos = -1.0
    return 1.0 - cos


def inner_product_distance(a, b):
    dot = 0.0
    for x, y in zip(a, b):
        dot += x * y
    return -dot


DISTANCE_FUNCTIONS = {
    "L2_DISTANCE": l2_distance,
    "L2": l2_distance,
    "EUCLIDEAN_DISTANCE": l2_distance,
    "COSINE_DISTANCE": cosine_distance,
    "COSINE": cosine_distance,
    "INNER_PRODUCT": inner_product_distance,
    "IP": inner_product_distance,
}


def get_distance_function(name):
    """Look up a distance function by name.

    Returns ``(canonical_name, callable)`` or raises for unknown names.
    """
    key = name.upper()
    if key in DISTANCE_FUNCTIONS:
        return key, DISTANCE_FUNCTIONS[key]
    raise SQLError(
        1305, "FUNCTION {} does not exist".format(name), "42000"
    )


# --- value formatting ------------------------------------------------------


def format_value(value):
    """Render a stored value the way the reference executor does.

    * ``None`` -> ``NULL``
    * ``int``  -> decimal
    * ``float`` -> shortest repr that round-trips, without a trailing
      ``.0`` for integral values (MySQL prints ``1`` for a float 1.0 in many
      cases, but keeps the float marker when the column type is float).
    * ``str``  -> as-is
    * ``tuple`` (vector) -> ``[1,2,3]``
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        if value == int(value) and abs(value) < 1e16:
            return str(int(value))
        return repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (tuple, list)):
        return "[" + ",".join(format_float(v) for v in value) + "]"
    return str(value)


def format_float(value):
    """Format a float vector element the way MySQL prints doubles."""
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def compare_values(left, right):
    """Three-way comparison used by ORDER BY.

    Returns a negative number, zero or a positive number.  Mixed types are
    compared by type rank first (BIGINT < VARCHAR) so ordering is total.
    """
    if left is None or right is None:
        if left is None and right is None:
            return 0
        return -1 if left is None else 1
    if isinstance(left, tuple) or isinstance(right, tuple):
        raise SQLError(
            1367, "cannot compare vector values", "22007"
        )
    if isinstance(left, str) != isinstance(right, str):
        return -1 if isinstance(left, (int, float)) else 1
    if isinstance(left, str):
        # Python compares str by Unicode code point, which is exactly the
        # rule required by the statement.
        if left < right:
            return -1
        if left > right:
            return 1
        return 0
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def values_equal(left, right):
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, tuple) or isinstance(right, tuple):
        if not (isinstance(left, tuple) and isinstance(right, tuple)):
            return False
        return len(left) == len(right) and all(
            x == y for x, y in zip(left, right)
        )
    return left == right
