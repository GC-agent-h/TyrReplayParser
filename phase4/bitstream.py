"""Phase 4: Iris/packet bitstream reader.

Replay packets go through UNetConnection::ReceivedRawPacket -> ReceivedPacket
(NetConnection.cpp:2002 / 2976). Replays are InternalAck (DemoNetDriver.cpp:4831,
SetInternalAck(true)) so there is NO FNetPacketNotify ack header; the packet body is
a sequence of bunches / Iris replication data read LSB-first via FBitReader.

For engine_net >= JitterInHeader (14) a per-packet info header precedes the data
(NetConnection.cpp:3075-3106):
  bHasPacketInfoPayload  (1 bit)
  if set: SerializeInt(jitter, MaxJitterClockTimeValue+1=1024) -> 10 bits
  bHasServerFrameTime    (1 bit)
  if set: 8 bits ServerFrameTime

For Iris replication, the data after the info header is an FReplicationReader::Read
stream (ReplicationReader.cpp:2846):
  [optional stream debug features]
  ObjectBatchCount = ReadBits(16)
  ReadObjectsPendingDestroy (16-bit count + handles)
  ReadObjects (object batches)
  ProcessHugeObject

FBitReader bit semantics: LSB-first. ReadBit = (Data[Pos>>3] >> (Pos&7)) & 1.
ReceivedRawPacket trims leading-zero (MSB-side) padding bits of the final byte:
  lz = count of leading zero bits (from bit7) in last byte; numbits = len*8 - lz.
SerializeIntPacked / SerializeIntPacked64: bit0 = more flag, payload = byte>>1,
7 bits per byte, LSB-first, read BIT-BY-BIT (works at any alignment).
"""

class BitReader:
    """Faithful LSB-first reimplementation of UE FBitReader (true bit reader)."""
    def __init__(self, data, num_bits):
        self.d = data
        self.numbits = num_bits
        self.pos = 0
        self.err = False
    def bits_left(self):
        return self.numbits - self.pos
    def read_bit(self):
        if self.pos >= self.numbits:
            self.err = True
            return 0
        byte = self.d[self.pos >> 3]
        bit = (byte >> (self.pos & 7)) & 1
        self.pos += 1
        return bit
    def read_int(self, maximum):
        """SerializeInt(Value, Max): NumBits = ceil(log2(Max)), LSB-first."""
        if maximum <= 1:
            return 0
        nb = (maximum - 1).bit_length()  # ceil(log2(maximum))
        v = 0
        for i in range(nb):
            v |= self.read_bit() << i
        return v
    def read_int_packed(self):
        """SerializeIntPacked (uint32): bit0=more, payload=byte>>1, 7 bits/byte LSB-first.
        Reads bit-by-bit so it works at any bit alignment (true FBitReader)."""
        value = 0
        shift = 0
        for _ in range(6):  # up to 42 bits (uint32 fits in 5 bytes)
            b = 0
            for i in range(8):
                b |= self.read_bit() << i
            if self.err:
                return value
            more = b & 1
            value |= (b >> 1) << shift
            if not more:
                break
            shift += 7
        return value
    def read_int_packed64(self):
        """SerializeIntPacked64 (uint64): same as above, up to 9 bytes."""
        value = 0
        shift = 0
        for _ in range(10):
            b = 0
            for i in range(8):
                b |= self.read_bit() << i
            if self.err:
                return value
            more = b & 1
            value |= (b >> 1) << shift
            if not more:
                break
            shift += 7
        return value
    def read_bits_bytes(self, nbits):
        """Read nbits (LSB-first) and return as bytes (padded). Bounds-guarded."""
        if nbits < 0 or nbits > self.numbits + 1024:
            self.err = True
            return b''
        nbytes = (nbits + 7) >> 3
        out = bytearray(nbytes)
        for i in range(nbits):
            if self.read_bit():
                out[i >> 3] |= (1 << (i & 7))
        return bytes(out)
    def read_fstring(self):
        """FString: int32 len; <0 UTF-16 (|len|*2 bytes), >0 ANSI (len bytes), 0 empty."""
        ln = self._read_int32()
        if ln == 0:
            return ""
        if abs(ln) > 1 << 20:
            self.err = True
            return ""
        if ln < 0:
            n = (-ln) * 2
            b = self.read_bits_bytes(n * 8)
            return b.decode('utf-16-le', errors='replace')
        else:
            b = self.read_bits_bytes(ln * 8)
            return b.split(b'\x00', 1)[0].decode('utf-8', errors='replace')
    def _read_int32(self):
        v = 0
        for i in range(32):
            v |= self.read_bit() << i
        return v
    def tell_bits(self):
        return self.pos
    def seek_bits(self, p):
        self.pos = p


def make_packet_reader(data):
    """Replicate ReceivedRawPacket's FBitReader construction (last-byte bit trim).
    Trims leading-zero (MSB-side) padding bits of the final byte. For a fully-zero
    last byte, trims all 8 bits (replay packets are padded)."""
    if len(data) == 0:
        return BitReader(data, 0)
    last = data[-1]
    lz = 0
    b = last
    while (b & 0x80) == 0 and lz < 8:
        b <<= 1
        lz += 1
    numbits = len(data) * 8 - lz
    if numbits < 0:
        numbits = 0
    return BitReader(data, numbits)


def read_stream_debug_features(br):
    """ReadReplicationDataStreamDebugFeatures: in non-debug builds reads 0 bits
    (value None). Return the feature enum or 0. Best-effort: peek 2 bits."""
    # EReplicationDataStreamDebugFeatures is a 2-bit enum in debug builds; in
    # shipping builds the writer emits nothing. We attempt to read it only if
    # the build would have written it; for replay parsing we assume None (0 bits).
    return 0


def parse_packet_info_header(br, engine_net=42):
    """Read the per-packet info header (NetConnection.cpp:3075). Returns dict or None."""
    if engine_net < 14:
        return {'has_info': False}
    b_has_info = br.read_bit()
    jitter = 0
    if b_has_info:
        jitter = br.read_int(1024)  # MaxJitterClockTimeValue+1 = 1024 -> 10 bits
    b_has_sft = br.read_bit()
    sft = 0
    if b_has_sft:
        sft = br.read_int(256)  # 8 bits
    return {'has_info': bool(b_has_info), 'jitter_ms': jitter,
            'has_server_frame_time': bool(b_has_sft), 'server_frame_time_ms': sft}


def parse_iris_envelope(br, engine_net=42):
    """Parse the top-level Iris FReplicationReader::Read envelope.
    Returns dict {object_batch_count, destroyed_objects:[handles], ...} or None.
    This decodes the header envelope; full object-batch content decode is large
    (see ReadObjectBatch/ReadObjectInBatch) and is the next step."""
    parse_packet_info_header(br, engine_net)
    if br.err:
        return None
    _ = read_stream_debug_features(br)
    if br.bits_left() < 16:
        return None
    object_batch_count = br.read_int(1 << 16)  # ReadBits(16)
    if br.err or object_batch_count >= 8192:
        return None
    destroyed = []
    if br.bits_left() >= 16:
        n_destroy = br.read_int(1 << 16)  # ReadObjectsPendingDestroy: 16-bit count
        if br.err:
            return None
        for _ in range(n_destroy):
            h = br.read_int_packed64()  # ReadNetRefHandleId
            if br.err:
                return None
            destroyed.append(h)
    return {'object_batch_count': object_batch_count, 'destroyed_objects': destroyed,
            'bits_left': br.bits_left(), 'pos': br.pos}


def parse_packet_auto(data, engine_net=42):
    """Best-effort: try to parse as Iris envelope; return (result, ok)."""
    br = make_packet_reader(data)
    res = parse_iris_envelope(br, engine_net)
    return res, (res is not None) and (not br.err)
