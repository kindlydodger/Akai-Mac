import Foundation
import CoreGraphics
import CoreText

let args = CommandLine.arguments
if args.count < 2 {
    fputs("usage: gen_boot_font <out_h_path>\n", stderr)
    exit(1)
}
let outPath = args[1]

let cellW = 10
let cellH = 18
let ctFont = CTFontCreateWithName("Menlo-Bold" as CFString, 14.5, nil)
let descent = CTFontGetDescent(ctFont)
let ascent = CTFontGetAscent(ctFont)
let baselineFromBottom = max(3.0, (CGFloat(cellH) - (ascent + descent)) * 0.5 + descent)
let cs = CGColorSpaceCreateDeviceGray()

var bytes = [UInt8]()
bytes.reserveCapacity(95 * cellW * cellH)

for code in 32...126 {
    var ch = UniChar(code)
    var glyph: CGGlyph = 0
    CTFontGetGlyphsForCharacters(ctFont, &ch, &glyph, 1)
    var adv = CGSize.zero
    CTFontGetAdvancesForGlyphs(ctFont, .horizontal, &glyph, &adv, 1)

    var buf = [UInt8](repeating: 0, count: cellW * cellH)
    if code != 32, let ctx = CGContext(data: &buf, width: cellW, height: cellH, bitsPerComponent: 8, bytesPerRow: cellW, space: cs, bitmapInfo: 0) {
        ctx.setAllowsAntialiasing(true)
        ctx.setShouldAntialias(true)
        ctx.setAllowsFontSmoothing(true)
        ctx.setShouldSmoothFonts(true)
        ctx.setFillColor(gray: 1.0, alpha: 1.0)
        let xOff = max(0.0, (CGFloat(cellW) - adv.width) * 0.5)
        var pos = CGPoint(x: xOff, y: baselineFromBottom)
        CTFontDrawGlyphs(ctFont, &glyph, &pos, 1, ctx)
    }
    bytes.append(contentsOf: buf)
}

var out = "// Auto-generated monospace Menlo-Bold ASCII bitmap table (10x18 per glyph, ASCII 32..126)\n"
out += "#pragma once\n#include <stdint.h>\n\n"
out += "#define BOOT_FONT_W \(cellW)\n"
out += "#define BOOT_FONT_H \(cellH)\n"
out += "#define BOOT_GRID_COLS 124\n"
out += "#define BOOT_GRID_ROWS 43\n"
out += "#define BOOT_GRID_X0 20\n"
out += "#define BOOT_GRID_Y0 13\n\n"
out += "static const uint8_t mpc_boot_font[95][BOOT_FONT_W * BOOT_FONT_H] = {\n"
for idx in 0..<95 {
    let chStr: String
    let c = idx + 32
    if c == 92 { chStr = "backslash" }
    else if c == 39 || c == 34 { chStr = "quote" }
    else { chStr = String(UnicodeScalar(c)!) }
    out += "  { // \(c) '\(chStr)'\n"
    let base = idx * cellW * cellH
    for y in 0..<cellH {
        let rowSlice = bytes[(base + y * cellW)..<(base + (y + 1) * cellW)]
        out += "    " + rowSlice.map { String($0) }.joined(separator: ",") + ",\n"
    }
    out += "  },\n"
}
out += "};\n"

try out.write(toFile: outPath, atomically: true, encoding: .utf8)
print("Generated \(outPath): 95 glyphs @ \(cellW)x\(cellH) (\(bytes.count) bytes)")
