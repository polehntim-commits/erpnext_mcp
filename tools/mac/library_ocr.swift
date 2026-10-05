// SPDX-License-Identifier: MIT
// Reference Library OCR on Tim's Mac — Apple Vision only (v0.238.0, decision 41).
//
//   swift tools/mac/library_ocr.swift scanned.pdf [3 5 7]
//
// Renders each page with PDFKit, reads it with Vision's accurate text recognizer (English and
// Spanish), and prints {"<page>": "<text>"} as JSON — the `page_texts` that `update_reference`
// takes. With page numbers, only those pages (list_references / get_reference name the pending
// ones). Nothing leaves the Mac except what you post.
import Foundation
import PDFKit
import Vision
import AppKit

let args = CommandLine.arguments.dropFirst()
guard let path = args.first, let document = PDFDocument(url: URL(fileURLWithPath: path)) else {
    FileHandle.standardError.write("usage: swift library_ocr.swift file.pdf [page …]\n".data(using: .utf8)!)
    exit(2)
}
let wanted = Set(args.dropFirst().compactMap(Int.init))
var out: [String: String] = [:]

for index in 0..<document.pageCount {
    let number = index + 1
    if !wanted.isEmpty && !wanted.contains(number) { continue }
    guard let page = document.page(at: index) else { continue }
    let bounds = page.bounds(for: .mediaBox)
    let scale: CGFloat = 2.5
    let image = page.thumbnail(of: CGSize(width: bounds.width * scale, height: bounds.height * scale), for: .mediaBox)
    guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["en-US", "es-MX"]
    request.usesLanguageCorrection = true
    try? VNImageRequestHandler(cgImage: cg).perform([request])
    let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
    out[String(number)] = lines.joined(separator: "\n")
}

let data = try JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys])
print(String(data: data, encoding: .utf8) ?? "{}")
