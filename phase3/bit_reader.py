"""Bit-level reader mirroring UE FBitReader for the replay's internal-ack
net serialization. The replay connection (IsInternalAck) writes export data
through a FBitWriter, so SerializeIntPacked packs bits LSB-first:
    repeat: read 7 value bits (LSB-first), then 1 'more' bit; stop when more==0.
This is byte-aligned-per-byte only when each group's more-bit is in bit 7.
"""

import struct


class BitReader:
    def __init__(self, data, bit_offset=0):
        self.d = data
        self.nbits = len(data) * 8
        self.bit = bit_offset

    def tell_bits(self):
        return self.bit

    def tell(self):
        return (self.bit + 7) // 8

    def at_end(self):
        return self.bit >= self.nbits

    def remaining_bits(self):
        return self.nbits - self.bit

    def _need_bits(self, k):
        if self.bit + k > self.nbits:
            raise EOFError(f"need {k} bits at bit {self.bit}, only {self.nbits - self.bit} left")

    def read_bits(self, nbits):
        """Read nbits LSB-first into an int."""
        self._need_bits(nbits)
        val = 0
        for i in range(nbits):
            byte = self.d[(self.bit + i) >> 3]
            bit = (byte >> ((self.bit + i) & 7)) & 1
            val |= bit << i
        self.bit += nbits
        return val

    def serialize_int_packed(self):
        """UE FBitReader::SerializeIntPacked (per-byte LSB-first: 7 value bits
        then 1 more bit)."""
        value = 0
        shift = 0
        while True:
            payload = self.read_bits(7)
            more = self.read_bits(1)
            value |= payload << shift
            if not more:
                break
            shift += 7
        return value

    def align_to_byte(self):
        if self.bit & 7:
            self.bit = (self.bit + 7) & ~7

    def u8(self):
        return self.read_bits(8)

    def u32(self):
        return self.read_bits(32)

    def i32(self):
        return struct.unpack('<i', struct.pack('<I', self.read_bits(32)))[0]

    def f32(self):
        return struct.unpack('<f', struct.pack('<I', self.read_bits(32)))[0]

    def i64(self):
        return self.read_bits(64)

    def raw(self, k):
        b = self.d[self.tell():self.tell() + k]
        self.bit += k * 8
        return b

    def fstring(self):
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:
            n = -n
            raw = self.raw(n * 2)
            return raw.decode('utf-16-le', errors='replace')
        raw = self.raw(n)
        return raw.decode('latin-1', errors='replace')


def test_bit_reader(data):
    """Walk the first frame's export section with the bit reader and report."""
    br = BitReader(data)
    li = br.i32()
    ts = br.f32()
    print(f"  LevelIndex={li} Time={ts:.2f} @bit {br.tell_bits()}")
    num = br.serialize_int_packed()
    print(f"  NumNetExports(bit)={num} @bit {br.tell_bits()} (byte 0x{br.tell():x})")
    for i in range(num):
        pos = br.tell()
        try:
            pi = br.serialize_int_packed()
            ne = br.serialize_int_packed()
        except Exception as e:
            print(f"  e{i} @0x{pos:x}: ERROoR reading pi/ne: {e}")
            break
        pn = None
        nxe = 0
        if ne:
            try:
                pn = br.fstring()
                nxe = br.serialize_int_packed()
            except Exception as e:
                print(f"  e{i} @0x{pos:x}: pi={pi} ne={ne} PN ERR: {e}")
                break
        try:
            fl = br.u8()
        except Exception as e:
            print(f"  e{i} @0x{pos:x}: pi={pi} ne={ne} pn={pn!r} FLAGS ERR: {e}")
            break
        h = c = nm = None
        bl = 0
        if fl & 1:
            try:
                h = br.serialize_int_packed()
                c = br.u32()
                nm = br.fstring()
                if fl & 2:
                    bl = br.serialize_int_packed()
                    br.raw(bl)
            except Exception as e:
                print(f"  e{i} @0x{pos:x}: pi={pi} ne={ne} pn={pn!r} flags={fl} EXPORT ERR: {e}")
                break
        if i < 10 or (pn is not None):
            print(f"  e{i} @0x{pos:x}: pi={pi} ne={ne} pn={pn!r} nxe={nxe} flags={fl} nm={nm!r} blob={bl}")
    print(f"  export section ends @byte 0x{br.tell():x} (bit {br.tell_bits()})")
    print(f"  next bytes: {data[br.tell():br.tell()+24].hex()}")
    return br


if __name__ == '__main__':
    import sys
    sys.path.insert(0, 'phase1')
    sys.path.insert(0, 'phase2')
    sys.path.insert(0, 'phase3')
    from replay_reader import ReplayReader
    from decompress import extract_payloads
    f = "Demos/TyrReplay1.replay"
    r = ReplayReader(f); r.parse_header(); r.parse_chunks()
    pl = extract_payloads(r)
    for name, c, raw, data, method in pl:
        if name == 'ReplayData':
            print("payload len", len(data))
            test_bit_reader(data)
            break
