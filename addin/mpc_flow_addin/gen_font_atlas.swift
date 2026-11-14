import Foundation
import CoreGraphics
import CoreText

let args = CommandLine.arguments
if args.count < 3 {
    fputs("usage: gen_font_atlas <ttf_path> <out_h_path>\n", stderr)
    exit(1)
}
let ttfPath = args[1]
let outPath = args[2]

let url = URL(fileURLWithPath: ttfPath) as CFURL
guard let dp = CGDataProvider(url: url), let cgFont = CGFont(dp) else {
    fputs("Failed to load font \(ttfPath)\n", stderr)
    exit(1)
}

struct GlyphInfo {
    var code: Int
    var w: Int
    var h: Int
    var adv: Int
    var topY: Int
    var offset: Int
}

var out = "// Auto-generated anti-aliased LiberationSans-Bold atlas\n#pragma once\n#include <stdint.h>\n\ntypedef struct { uint8_t w, h, adv; int8_t yoff; uint32_t off; } FlowGlyph;\n\n"
let sizes: [(String, CGFloat)] = [("14", 14.0), ("19", 19.0), ("25", 25.0)]
let cs = CGColorSpaceCreateDeviceGray()

for (tag, sz) in sizes {
    let ctFont = CTFontCreateWithGraphicsFont(cgFont, sz, nil, nil)
    let ascent = Int(ceil(CTFontGetAscent(ctFont)))
    let boxW = Int(ceil(sz * 1.4)) + 4
    let boxH = Int(ceil(sz * 1.6)) + 4
    let baseline = boxH - 4 - Int(ceil(CTFontGetDescent(ctFont)))
    var bytes = [UInt8]()
    var glyphs = [GlyphInfo]()

    for code in 32...126 {
        let scalar = UnicodeScalar(code)!
        var ch = UniChar(scalar.value)
        var glyph: CGGlyph = 0
        CTFontGetGlyphsForCharacters(ctFont, &ch, &glyph, 1)
        var adv = CGSize.zero
        CTFontGetAdvancesForGlyphs(ctFont, .horizontal, &glyph, &adv, 1)
        let advInt = max(3, Int(round(adv.width)))

        var buf = [UInt8](repeating: 0, count: boxW * boxH)
        if code != 32, let ctx = CGContext(data: &buf, width: boxW, height: boxH, bitsPerComponent: 8, bytesPerRow: boxW, space: cs, bitmapInfo: 0) {
            ctx.setAllowsAntialiasing(true)
            ctx.setShouldAntialias(true)
            ctx.setFillColor(gray: 1.0, alpha: 1.0)
            var pos = CGPoint(x: 1, y: boxH - baseline)
            CTFontDrawGlyphs(ctFont, &glyph, &pos, 1, ctx)
        }
        // CoreGraphics C-buffer index [0..boxW-1] is already top scanline; find tight bbox
        let td = buf
        var minX = boxW, maxX = -1, minY = boxH, maxY = -1
        for y in 0..<boxH {
            for x in 0..<boxW {
                if td[y * boxW + x] > 4 {
                    if x < minX { minX = x }
                    if x > maxX { maxX = x }
                    if y < minY { minY = y }
                    if y > maxY { maxY = y }
                }
            }
        }
        let off = bytes.count
        if maxX >= minX && maxY >= minY {
            let gw = maxX - minX + 1
            let gh = maxY - minY + 1
            let yoff = minY - (baseline - ascent)
            for y in minY...maxY {
                for x in minX...maxX {
                    bytes.append(td[y * boxW + x])
                }
            }
            glyphs.append(GlyphInfo(code: code, w: gw, h: gh, adv: advInt, topY: yoff, offset: off))
        } else {
            glyphs.append(GlyphInfo(code: code, w: 0, h: 0, adv: advInt, topY: 0, offset: off))
        }
    }

    out += "static const uint8_t flow_atlas_\(tag)_data[] = {\n"
    for idx in stride(from: 0, to: bytes.count, by: 24) {
        let end = min(bytes.count, idx + 24)
        out += "  " + bytes[idx..<end].map { String($0) }.joined(separator: ",") + ",\n"
    }
    if bytes.isEmpty { out += "  0,\n" }
    out += "};\n\nstatic const FlowGlyph flow_atlas_\(tag)_glyphs[95] = {\n"
    for g in glyphs {
        out += "  {\(g.w), \(g.h), \(g.adv), \(g.topY), \(g.offset)},\n"
    }
    out += "};\n\n"
}

try out.write(toFile: outPath, atomically: true, encoding: .utf8)
print("Generated \(outPath) (\(out.utf8.count) bytes)")
