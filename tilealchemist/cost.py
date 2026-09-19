"""What one manifest record costs a worker; see docs/ARCHITECTURE.md "Parallelism"."""

# Fitted against a planet run's 128 worker durations: decode outgrows byte length.
DENSITY_EXPONENT = 1.5

# Entries, decode work, output tiles, as shares of one worker's budget.
AXIS_SHARES = (0.25, 0.60, 0.15)


def _axis_totals(records):
    work_total = 0.0
    tile_total = 0.0
    previous_key = None
    for record in records:
        key = (record.offset, record.length)
        if key != previous_key:
            work_total += record.length ** DENSITY_EXPONENT
            previous_key = key
        tile_total += record.run_length
    return work_total, tile_total


def _weights(records, entry_scale, work_scale, tile_scale):
    previous_key = None
    for record in records:
        key = (record.offset, record.length)
        work = 0.0
        if key != previous_key:
            work = record.length ** DENSITY_EXPONENT
            previous_key = key
        yield entry_scale + work_scale * work + tile_scale * record.run_length


def cost_weights(records):
    """Every record's share of the run's cost, and what those shares add up to."""
    work_total, tile_total = _axis_totals(records)
    entry_share, work_share, tile_share = AXIS_SHARES
    scales = (entry_share / len(records) if records else 0.0,
              work_share / work_total if work_total else 0.0,
              tile_share / tile_total if tile_total else 0.0)
    total = sum(share for share, scale in zip(AXIS_SHARES, scales) if scale)
    return _weights(records, *scales), total
