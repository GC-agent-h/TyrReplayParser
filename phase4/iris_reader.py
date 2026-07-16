"""Phase 4: Iris replication packet reader (structural decode).

Format reverse-engineered from UE 5.6.1 source (FReplicationReader::Read,
ReplicationReader.cpp:2846). The replay stores the Iris FReplicationReader::Read
stream directly (no UE packet-info header, no legacy bunch header). Envelope:

    ObjectBatchCount      = ReadBits(16)               # top-level batch count
    [destroyed objects]   = ReadBits(16) count + handles (SerializeIntPacked64)
    for each batch (ObjectBatchCount):
        bIsDestructionInfo = ReadBool()
        if bIsDestructionInfo:
            ReadAndExecuteDestructionInfo + sentinel (debug only) -> 1 object
        else:
            ReadSentinel("Object")   # no-op in shipping build
            Handle        = ReadNetRefHandleId()  # packed uint64
            BatchSize     = ReadBits(NumBitsUsedForBatchSize)
            bHasBatchOwnerData = ReadBool()
            bHasExports        = ReadBool()
            if bHasExports:
                seek to BatchEndOrStartOfExportsPos (= pos + BatchSize)
                ObjectReferenceCache->ReadExports(...)   # variable
                seek back to ReturnPos
            # object data occupies [ReturnPos, BatchEndOrStartOfExportsPos)
            # then exports occupy [BatchEndOrStartOfExportsPos, BatchEndPos)
            next batch begins at BatchEndPos

The BatchSize lets us tile batches WITHOUT protocol descriptors: after reading a
batch's header + (optionally) its exports, the next batch starts at BatchEndPos.

Per-object state (changemask + properties) requires the game's FReplicationProtocol
descriptors (in the TYR SDK dump) and is decoded in Phase 5.
"""

import bitstream as bs


def read_net_ref_handle_id(br):
    """ReadNetRefHandleId: SerializeIntPacked64 (packed uint64)."""
    return br.read_int_packed64()


def walk_batches(br, object_batch_count, num_bits_batch_size=13):
    """Walk `object_batch_count` batch HEADERS using BatchSize delimiting.
    Structural-only: does NOT decode per-object property state, just walks
    handles/flags and uses BatchSize to advance to the next batch. Returns
    (batches, ok, errmsg)."""
    batches = []
    for bi in range(object_batch_count):
        if br.err or br.bits_left() <= 0:
            return batches, False, "ran out at batch %d (of %d)" % (bi, object_batch_count)
        b_is_destruction = br.read_bit()
        if b_is_destruction:
            h = read_net_ref_handle_id(br)
            batches.append({'type': 'destruction', 'handle': h, 'pos': br.pos})
            continue
        handle = read_net_ref_handle_id(br)
        if br.err:
            return batches, False, "handle read err at batch %d" % bi
        batch_size = br.read_int(1 << num_bits_batch_size)
        b_has_owner = br.read_bit()
        b_has_exports = br.read_bit()
        return_pos = br.pos
        batch_end_or_exports = return_pos + batch_size
        # We cannot decode state/exports without protocol descriptors, so we
        # seek BatchSize bits forward for the state region. This lets us verify
        # the envelope + header tiling structurally.
        br.seek_bits(batch_end_or_exports)
        if br.err:
            return batches, False, "seek err at batch %d (batch_size=%d)" % (bi, batch_size)
        batches.append({'type': 'object', 'handle': handle,
                        'batch_size': batch_size, 'has_owner': bool(b_has_owner),
                        'has_exports': bool(b_has_exports), 'pos': return_pos})
    return batches, True, ""


def parse_iris_stream(data, num_bits_batch_size=13):
    """Parse a raw Iris FReplicationReader::Read stream (no UE packet header).
    data: bytes of the concatenated frame's Iris packet stream.
    Returns (info_dict, ok). Structural success = envelope read + all batch
    headers walked without bitstream error."""
    br = bs.make_packet_reader(data)
    if br.bits_left() < 16:
        return None, False
    object_batch_count = br.read_int(1 << 16)
    if object_batch_count == 0 or object_batch_count >= 8192:
        return {'object_batch_count': object_batch_count, 'batches': [],
                'bits_left': br.bits_left(), 'pos': br.pos}, False
    n_destroy = 0
    destroyed = []
    if br.bits_left() >= 16:
        n_destroy = br.read_int(1 << 16)
        if n_destroy < 8192 and not br.err:
            for _ in range(n_destroy):
                destroyed.append(read_net_ref_handle_id(br))
                if br.err:
                    break
    batches, ok, err = walk_batches(br, object_batch_count,
                                    num_bits_batch_size=num_bits_batch_size)
    info = {'object_batch_count': object_batch_count, 'destroyed': destroyed,
            'batches': batches, 'bits_left': br.bits_left(), 'pos': br.pos, 'err': err}
    return info, (ok and not br.err)
