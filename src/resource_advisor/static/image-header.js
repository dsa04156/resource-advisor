/** Read a bounded header before allocating a decoded bitmap. Reject incomplete headers. */
export function imageDimensions(buffer, mime) {
  const bytes = new Uint8Array(buffer),
    view = new DataView(buffer);
  const at = (offset, length) => offset >= 0 && offset + length <= bytes.length;
  const text = (offset, value) =>
    at(offset, value.length) &&
    [...value].every((c, i) => bytes[offset + i] === c.charCodeAt(0));
  const invalid = () => {
    throw new Error("지원하는 이미지 헤더를 확인할 수 없어요.");
  };
  const result = (width, height) => {
    if (!width || !height) return invalid();
    return { width, height };
  };
  if (mime === "image/png") {
    if (
      !at(0, 33) ||
      ![137, 80, 78, 71, 13, 10, 26, 10].every((b, i) => bytes[i] === b) ||
      view.getUint32(8) !== 13 ||
      !text(12, "IHDR")
    )
      return invalid();
    return result(view.getUint32(16), view.getUint32(20));
  }
  if (mime === "image/jpeg") {
    if (!at(0, 2) || bytes[0] !== 255 || bytes[1] !== 216) return invalid();
    let offset = 2;
    const frames = new Set([
      0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce,
      0xcf,
    ]);
    while (at(offset, 1)) {
      if (bytes[offset++] !== 255) return invalid();
      while (at(offset, 1) && bytes[offset] === 255) offset++;
      if (!at(offset, 1)) return invalid();
      const marker = bytes[offset++];
      if (marker === 0xda || marker === 0xd9) return invalid();
      if (marker === 1 || (marker >= 0xd0 && marker <= 0xd7)) continue;
      if (!at(offset, 2)) return invalid();
      const length = view.getUint16(offset);
      if (length < 2 || !at(offset, length)) return invalid();
      if (frames.has(marker)) {
        if (length < 8) return invalid();
        return result(view.getUint16(offset + 5), view.getUint16(offset + 3));
      }
      offset += length;
    }
    return invalid();
  }
  if (mime === "image/webp") {
    if (!at(0, 12) || !text(0, "RIFF") || !text(8, "WEBP")) return invalid();
    const uint24 = (i) => bytes[i] + bytes[i + 1] * 256 + bytes[i + 2] * 65536;
    for (let offset = 12; at(offset, 8); ) {
      const length = view.getUint32(offset + 4, true),
        data = offset + 8;
      if (text(offset, "VP8X") && length >= 10 && at(data, 10)) {
        if (bytes[data] & 2) return invalid(); // Animated inputs are outside the single-image contract.
        return result(uint24(data + 4) + 1, uint24(data + 7) + 1);
      }
      if (
        text(offset, "VP8L") &&
        length >= 5 &&
        at(data, 5) &&
        bytes[data] === 0x2f
      ) {
        const bits = view.getUint32(data + 1, true);
        return result((bits & 0x3fff) + 1, ((bits >>> 14) & 0x3fff) + 1);
      }
      if (
        text(offset, "VP8 ") &&
        length >= 10 &&
        at(data, 10) &&
        !(bytes[data] & 1) &&
        text(data + 3, "\u009d\u0001\u002a")
      )
        return result(
          view.getUint16(data + 6, true) & 0x3fff,
          view.getUint16(data + 8, true) & 0x3fff,
        );
      if (!at(data, length)) return invalid();
      offset = data + length + (length & 1);
    }
  }
  return invalid();
}
